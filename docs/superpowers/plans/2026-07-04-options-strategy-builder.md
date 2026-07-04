# Options Strategy Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Builder tab where the user hand-builds a multi-leg options structure (strikes/expiries from the live chain), sees a Black-Scholes payoff diagram, saves it as a tracked paper trade with target/stop exits, and a background monitor keeps its P&L updated and paper-closes it on target/stop — with no orders ever transmitted.

**Architecture:** Pure logic (Black-Scholes + generic combo valuation/payoff) has no I/O and is unit-tested in isolation. A new `BuilderPositionRecord` table stores saved trades. IBKR is used read-only (option chain + leg mids). A single app-lifespan monitor task revalues open trades from live mids and paper-closes on target/stop. A FastAPI router exposes chain/quote/payoff/trade CRUD. A React Builder tab drives it.

**Tech Stack:** Python 3.14, FastAPI, async SQLAlchemy (SQLite dev), ib_async (read-only), pytest; React + Vite + Recharts.

## Global Constraints

- **No order transmission.** `app/engine/execution.submit_combo_order` and any live/paper order path MUST NOT be called anywhere in this feature. IBKR is read-only (quotes + chain).
- **Venv Python:** run everything with `../.venv/bin/python` from `backend/` (repo venv is 3.14).
- **Test convention:** pytest-style tests (`@pytest.mark.asyncio` for async). DB-touching tests set `DATABASE_URL=sqlite+aiosqlite:///./_t_<name>.db` and call `await init_db()` first. Run e.g. `DATABASE_URL=sqlite+aiosqlite:///./_t_x.db ../.venv/bin/python -m pytest tests/<file>.py -q`.
- **Per-share vs dollars:** stored/computed option values are per-share (per 1x); dollar figures are `× 100` at the boundary (P&L in dollars). Matches the Triple Calendar store.
- **Exit rule:** target/stop are fractions of `net_debit` and apply ONLY to debit structures (`net_debit > 0`). Credit structures track P&L but never %-auto-close.
- **Leg shape (canonical, used everywhere):** `{"action": "BUY"|"SELL", "right": "C"|"P", "strike": float, "expiry": "YYYY-MM-DD", "qty": int}`.
- Match existing style: `from __future__ import annotations`, module docstrings, `logger = logging.getLogger(__name__)` where logging is used.

---

### Task 1: Black-Scholes pricer (pure)

**Files:**
- Create: `backend/app/options/__init__.py` (empty)
- Create: `backend/app/options/blackscholes.py`
- Test: `backend/tests/test_options_bs.py`

**Interfaces:**
- Produces:
  - `norm_cdf(x: float) -> float`
  - `bs_price(S: float, K: float, T: float, iv: float, r: float, kind: str) -> float` — European price; `kind` is `"C"` or `"P"`; `T` in years; `T<=0` or `iv<=0` → intrinsic. No dividends (v1).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_options_bs.py
import math

from app.options.blackscholes import bs_price, norm_cdf


def test_norm_cdf_center_and_tails():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-9
    assert norm_cdf(-8) < 1e-6 and norm_cdf(8) > 1 - 1e-6


def test_expiry_is_intrinsic():
    assert bs_price(110, 100, 0, 0.2, 0.04, "C") == 10
    assert bs_price(90, 100, 0, 0.2, 0.04, "P") == 10
    assert bs_price(90, 100, 0, 0.2, 0.04, "C") == 0


def test_put_call_parity():
    S, K, T, iv, r = 100, 100, 0.5, 0.25, 0.04
    c = bs_price(S, K, T, iv, r, "C")
    p = bs_price(S, K, T, iv, r, "P")
    # C - P == S - K*e^{-rT}
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-6
    assert c > 0 and p > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_options_bs.py -q`
Expected: FAIL — `ModuleNotFoundError: app.options.blackscholes`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/options/__init__.py
```
(empty file)

```python
# backend/app/options/blackscholes.py
"""Pure Black-Scholes pricer (European, no dividends). No I/O — unit-tested in isolation.
Used by the options builder's payoff diagram to value still-open legs at a target date."""
from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, iv: float, r: float, kind: str) -> float:
    """European call/put price. T in years; T<=0 or iv<=0 collapses to intrinsic value."""
    kind = kind.upper()
    if T <= 0 or iv <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if kind == "C":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_options_bs.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/__init__.py backend/app/options/blackscholes.py backend/tests/test_options_bs.py
git commit -m "feat(builder): pure Black-Scholes pricer for payoff diagrams"
```

---

### Task 2: Generic combo valuation + payoff (pure)

**Files:**
- Create: `backend/app/options/combo.py`
- Test: `backend/tests/test_options_combo.py`

**Interfaces:**
- Consumes: `app.options.blackscholes.bs_price`.
- Produces:
  - `LEG_SIGN = {"BUY": 1, "SELL": -1}`
  - `combo_value(legs: list[dict], mids: list[float | None]) -> float | None` — `Σ sign(action)·qty·mid`, aligned by index; `None` if any mid is `None`.
  - `payoff_at(legs: list[dict], S: float, eval_date: date, iv: float, r: float) -> float` — structure value at underlying `S` on `eval_date` (each leg BS-valued; expired legs → intrinsic).
  - `payoff_curve(legs, net_debit, eval_date, iv, r, lo, hi, points=61) -> list[dict]` — `[{price, pnl}]` where `pnl=(payoff_at−net_debit)·100`.
  - `curve_stats(curve) -> dict` — `{max_profit, max_loss, breakevens: list[float]}`.
  - `exit_check(net_debit, value, target_pct, stop_pct) -> str | None` — `"target"`/`"stop"`/`None`; only fires for `net_debit>0`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_options_combo.py
from datetime import date

from app.options.combo import (combo_value, curve_stats, exit_check, payoff_at,
                               payoff_curve)

NEAR = "2026-07-24"
FAR = "2026-07-31"


def leg(action, right, strike, expiry, qty=1):
    return {"action": action, "right": right, "strike": strike, "expiry": expiry, "qty": qty}


def test_combo_value_signs_and_missing():
    legs = [leg("BUY", "C", 100, FAR), leg("SELL", "C", 100, NEAR)]
    assert combo_value(legs, [3.0, 1.0]) == 2.0     # +3 -1
    assert combo_value(legs, [3.0, None]) is None    # missing mid


def test_vertical_payoff_is_clean_intrinsic_at_expiry():
    # Bull call debit spread, both legs expire NEAR.
    legs = [leg("BUY", "C", 100, NEAR), leg("SELL", "C", 105, NEAR)]
    ev = date.fromisoformat(NEAR)
    # At expiry the far leg (BS) collapses to intrinsic; check max value == width.
    assert abs(payoff_at(legs, 120, ev, 0.25, 0.04) - 5.0) < 1e-6   # 20 - 15
    assert abs(payoff_at(legs, 90, ev, 0.25, 0.04) - 0.0) < 1e-6


def test_calendar_has_time_value_at_near_expiry():
    # Long far call, short near call, same strike → calendar. At the NEAR expiry the short
    # leg is worthless ATM but the long (far) leg still holds BS time value → positive tent.
    legs = [leg("BUY", "C", 100, FAR), leg("SELL", "C", 100, NEAR)]
    ev = date.fromisoformat(NEAR)
    assert payoff_at(legs, 100, ev, 0.25, 0.04) > 0.0


def test_curve_and_stats():
    legs = [leg("BUY", "C", 100, NEAR), leg("SELL", "C", 105, NEAR)]
    curve = payoff_curve(legs, net_debit=2.0, eval_date=date.fromisoformat(NEAR),
                         iv=0.25, r=0.04, lo=90, hi=115, points=51)
    assert curve[0]["pnl"] < 0 < curve[-1]["pnl"]     # loss below, profit above
    stats = curve_stats(curve)
    assert stats["max_profit"] > 0 and stats["max_loss"] < 0
    assert any(98 <= be <= 104 for be in stats["breakevens"])  # BE near 102 (100+2 debit)


def test_exit_check_debit_only():
    assert exit_check(2.0, 2.2, 0.10, 0.25) == "target"   # +10%
    assert exit_check(2.0, 1.5, 0.10, 0.25) == "stop"     # -25%
    assert exit_check(2.0, 2.1, 0.10, 0.25) is None
    assert exit_check(-2.0, 5.0, 0.10, 0.25) is None      # credit: no %-exit
    assert exit_check(2.0, None, 0.10, 0.25) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_options_combo.py -q`
Expected: FAIL — `ModuleNotFoundError: app.options.combo`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/options/combo.py
"""Generic multi-leg options math (pure): current value from mids, and Black-Scholes payoff
curves. Structure-agnostic — works for any legs, unlike the calendar-specific TC logic."""
from __future__ import annotations

from datetime import date

from app.options.blackscholes import bs_price

LEG_SIGN = {"BUY": 1, "SELL": -1}


def _qty(leg: dict) -> int:
    return int(leg.get("qty", 1) or 1)


def _expiry(leg: dict) -> date:
    e = leg["expiry"]
    return e if isinstance(e, date) else date.fromisoformat(e)


def combo_value(legs: list[dict], mids: list[float | None]) -> float | None:
    """Per-1x value of the structure from leg mids (aligned by index). None if any mid missing.
    This is the entry net-debit at save (entry mids) and the live value at monitor time."""
    total = 0.0
    for leg, mid in zip(legs, mids):
        if mid is None:
            return None
        total += LEG_SIGN[leg["action"]] * _qty(leg) * mid
    return total


def payoff_at(legs: list[dict], S: float, eval_date: date, iv: float, r: float) -> float:
    """Structure value (per 1x) at underlying price S on eval_date. Expired legs collapse to
    intrinsic; still-open legs are Black-Scholes-valued (so calendars keep their tent)."""
    total = 0.0
    for leg in legs:
        T = max((_expiry(leg) - eval_date).days, 0) / 365.0
        price = bs_price(S, leg["strike"], T, iv, r, leg["right"])
        total += LEG_SIGN[leg["action"]] * _qty(leg) * price
    return total


def payoff_curve(legs: list[dict], net_debit: float, eval_date: date, iv: float, r: float,
                 lo: float, hi: float, points: int = 61) -> list[dict]:
    """[{price, pnl}] across [lo, hi]; pnl in dollars = (value - net_debit) * 100."""
    if points < 2:
        points = 2
    step = (hi - lo) / (points - 1)
    out = []
    for i in range(points):
        S = lo + step * i
        val = payoff_at(legs, S, eval_date, iv, r)
        out.append({"price": round(S, 2), "pnl": round((val - net_debit) * 100, 2)})
    return out


def curve_stats(curve: list[dict]) -> dict:
    """Max profit/loss and linear-interpolated breakeven crossings from a payoff curve."""
    pnls = [p["pnl"] for p in curve]
    breakevens = []
    for a, b in zip(curve, curve[1:]):
        if (a["pnl"] <= 0 <= b["pnl"]) or (a["pnl"] >= 0 >= b["pnl"]):
            if b["pnl"] != a["pnl"]:
                t = -a["pnl"] / (b["pnl"] - a["pnl"])
                breakevens.append(round(a["price"] + t * (b["price"] - a["price"]), 2))
    return {"max_profit": max(pnls), "max_loss": min(pnls), "breakevens": breakevens}


def exit_check(net_debit: float | None, value: float | None,
               target_pct: float | None, stop_pct: float | None) -> str | None:
    """'target' | 'stop' | None. Only debit structures (net_debit > 0) auto-exit on %."""
    if net_debit is None or net_debit <= 0 or value is None:
        return None
    pnl_pct = (value - net_debit) / net_debit
    if target_pct is not None and pnl_pct >= target_pct:
        return "target"
    if stop_pct is not None and pnl_pct <= -stop_pct:
        return "stop"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_options_combo.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/combo.py backend/tests/test_options_combo.py
git commit -m "feat(builder): generic combo valuation + Black-Scholes payoff curve"
```

---

### Task 3: BuilderPositionRecord model

**Files:**
- Modify: `backend/app/db/models.py` (add class after `OptionPositionRecord`, before `SystemFlag`)
- Test: `backend/tests/test_builder_model.py`

**Interfaces:**
- Produces: ORM `BuilderPositionRecord` with columns: `id:int`, `underlying:str`, `legs:JSON`, `net_debit:float`, `iv:float`, `target_pct:float|None`, `stop_pct:float|None`, `status:str='open'`, `entry_time:datetime`, `exit_time:datetime|None`, `exit_reason:str|None`, `pnl:float|None`, `last_value:float|None`, `last_pnl:float|None`, `last_pnl_pct:float|None`, `valued_at:datetime|None`. Table `builder_positions`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_model.py
import pytest

from app.db.base import init_db, session_scope
from app.db.models import BuilderPositionRecord


@pytest.mark.asyncio
async def test_builder_record_round_trips():
    await init_db()  # create_all makes the new table; no migration needed
    async with session_scope() as s:
        rec = BuilderPositionRecord(
            underlying="QQQ",
            legs=[{"action": "BUY", "right": "C", "strike": 500.0,
                   "expiry": "2026-07-31", "qty": 1}],
            net_debit=4.2, iv=0.25, target_pct=0.10, stop_pct=0.25)
        s.add(rec)
        await s.flush()
        rid = rec.id
    async with session_scope() as s:
        got = await s.get(BuilderPositionRecord, rid)
        assert got.underlying == "QQQ" and got.status == "open"
        assert got.legs[0]["strike"] == 500.0 and got.net_debit == 4.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_bm.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bm.db ../.venv/bin/python -m pytest tests/test_builder_model.py -q; rm -f _t_bm.db`
Expected: FAIL — `ImportError: cannot import name 'BuilderPositionRecord'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/db/models.py`, add immediately before `class SystemFlag(Base):`:

```python
class BuilderPositionRecord(Base):
    """A user-built, tracked-only (paper) multi-leg options trade. No orders are ever
    transmitted; a monitor revalues it from live mids and paper-closes on target/stop."""

    __tablename__ = "builder_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(16))
    legs: Mapped[list] = mapped_column(JSON, default=list)   # canonical leg dicts
    net_debit: Mapped[float] = mapped_column(Float, default=0.0)  # per 1x; debit>0, credit<0
    iv: Mapped[float] = mapped_column(Float, default=0.25)   # IV used for the saved payoff
    target_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(8), nullable=True)  # target|stop|manual
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)          # realized $ at close
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)   # per 1x
    last_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)     # $ unrealized
    last_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    valued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_bm.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bm.db ../.venv/bin/python -m pytest tests/test_builder_model.py -q; rm -f _t_bm.db`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/tests/test_builder_model.py
git commit -m "feat(builder): BuilderPositionRecord table for tracked paper trades"
```

---

### Task 4: Builder store (persist / list / value / close)

**Files:**
- Create: `backend/app/options/store.py`
- Test: `backend/tests/test_builder_store.py`

**Interfaces:**
- Consumes: `BuilderPositionRecord`, `session_scope`.
- Produces (all async):
  - `save_trade(underlying, legs, net_debit, iv, target_pct, stop_pct) -> int` (new id)
  - `list_trades(status: str | None = None) -> list[BuilderPositionRecord]` (open first, then newest)
  - `list_open() -> list[BuilderPositionRecord]`
  - `get_trade(trade_id) -> BuilderPositionRecord | None`
  - `update_valuation(trade_id, value, pnl, pnl_pct) -> None` (sets `last_*` + `valued_at`)
  - `close_trade(trade_id, pnl, reason) -> None` (sets `status="closed"`, `pnl`, `exit_time`, `exit_reason`)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_store.py
import pytest

from app.db.base import init_db
from app.options import store

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0, "expiry": "2026-07-31", "qty": 1}]


@pytest.mark.asyncio
async def test_save_list_value_close():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)
    assert (await store.get_trade(tid)).status == "open"
    assert any(t.id == tid for t in await store.list_open())

    await store.update_valuation(tid, value=2.2, pnl=20.0, pnl_pct=0.10)
    t = await store.get_trade(tid)
    assert t.last_value == 2.2 and t.last_pnl == 20.0 and t.valued_at is not None

    await store.close_trade(tid, pnl=20.0, reason="target")
    t = await store.get_trade(tid)
    assert t.status == "closed" and t.exit_reason == "target" and t.exit_time is not None
    assert all(o.id != tid for o in await store.list_open())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_bs.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bs.db ../.venv/bin/python -m pytest tests/test_builder_store.py -q; rm -f _t_bs.db`
Expected: FAIL — `ModuleNotFoundError: app.options.store`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/options/store.py
"""Persistence for builder (tracked paper) trades. No order side effects — pure DB."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import BuilderPositionRecord


async def save_trade(underlying: str, legs: list[dict], net_debit: float, iv: float,
                     target_pct: float | None, stop_pct: float | None) -> int:
    async with session_scope() as session:
        rec = BuilderPositionRecord(
            underlying=underlying.upper(), legs=list(legs), net_debit=net_debit, iv=iv,
            target_pct=target_pct, stop_pct=stop_pct, status="open")
        session.add(rec)
        await session.flush()
        return rec.id


async def list_trades(status: str | None = None) -> list[BuilderPositionRecord]:
    async with session_scope() as session:
        stmt = select(BuilderPositionRecord)
        if status:
            stmt = stmt.where(BuilderPositionRecord.status == status)
        # open first, then newest by entry.
        stmt = stmt.order_by(BuilderPositionRecord.status.desc(),
                             BuilderPositionRecord.entry_time.desc())
        return list((await session.execute(stmt)).scalars().all())


async def list_open() -> list[BuilderPositionRecord]:
    return await list_trades(status="open")


async def get_trade(trade_id: int) -> BuilderPositionRecord | None:
    async with session_scope() as session:
        return await session.get(BuilderPositionRecord, trade_id)


async def update_valuation(trade_id: int, value: float, pnl: float, pnl_pct: float) -> None:
    async with session_scope() as session:
        rec = await session.get(BuilderPositionRecord, trade_id)
        if rec and rec.status == "open":
            rec.last_value = value
            rec.last_pnl = pnl
            rec.last_pnl_pct = pnl_pct
            rec.valued_at = datetime.now(timezone.utc)


async def close_trade(trade_id: int, pnl: float | None, reason: str) -> None:
    async with session_scope() as session:
        rec = await session.get(BuilderPositionRecord, trade_id)
        if rec and rec.status == "open":
            rec.status = "closed"
            rec.pnl = pnl
            rec.exit_reason = reason
            rec.exit_time = datetime.now(timezone.utc)
```

Note: `status.desc()` puts `"open"` before `"closed"` alphabetically-reversed (`o` > `c`). Good.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_bs.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bs.db ../.venv/bin/python -m pytest tests/test_builder_store.py -q; rm -f _t_bs.db`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/store.py backend/tests/test_builder_store.py
git commit -m "feat(builder): store for tracked paper trades"
```

---

### Task 5: Option chain + leg mids (IBKR read-only)

**Files:**
- Create: `backend/app/options/chain.py`
- Test: `backend/tests/test_builder_chain.py`

**Interfaces:**
- Consumes: `app.ibkr.contracts.stock`, `app.ibkr.gateway.get_gateway`, `app.strategies.triple_calendar.live_source.LivePriceSource`.
- Produces (async):
  - `get_option_chain(underlying) -> dict` → `{"underlying": str, "expirations": [ISO...], "strikes": [float...]}` (both sorted).
  - `get_leg_mids(underlying, legs) -> list[float | None]` — live mid per leg (index-aligned), concurrent.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_chain.py
import pytest

from app.options import chain


class _Chain:
    exchange = "SMART"
    expirations = {"20260731", "20260724", "20260807"}
    strikes = {495.0, 500.0, 505.0}


class _FakeIB:
    async def qualifyContractsAsync(self, *a):
        return list(a)

    async def reqSecDefOptParamsAsync(self, symbol, ftp, sectype, conid):
        return [_Chain()]


@pytest.mark.asyncio
async def test_get_option_chain_sorts_and_isoformats(monkeypatch):
    class _GW:
        async def get_ib(self):
            return _FakeIB()
    monkeypatch.setattr(chain, "get_gateway", lambda: _GW())
    out = await chain.get_option_chain("qqq")
    assert out["underlying"] == "QQQ"
    assert out["expirations"] == ["2026-07-24", "2026-07-31", "2026-08-07"]
    assert out["strikes"] == [495.0, 500.0, 505.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_builder_chain.py -q`
Expected: FAIL — `ModuleNotFoundError: app.options.chain`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/options/chain.py
"""Read-only IBKR access for the builder: the underlying's option chain (available expiries
+ strikes) and per-leg live mids. No orders — quotes/definitions only."""
from __future__ import annotations

import asyncio
from datetime import date

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.strategies.triple_calendar.live_source import LivePriceSource


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


async def get_option_chain(underlying: str) -> dict:
    ib = await get_gateway().get_ib()
    u = stock(underlying)
    await ib.qualifyContractsAsync(u)
    chains = await ib.reqSecDefOptParamsAsync(u.symbol, "", u.secType, u.conId)
    ch = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
    if ch is None:
        return {"underlying": underlying.upper(), "expirations": [], "strikes": []}
    return {
        "underlying": underlying.upper(),
        "expirations": sorted(_iso(str(e)) for e in ch.expirations),
        "strikes": sorted(float(s) for s in ch.strikes),
    }


async def get_leg_mids(underlying: str, legs: list[dict]) -> list[float | None]:
    src = LivePriceSource()
    return list(await asyncio.gather(*[
        src.aoption_mid(underlying, date.fromisoformat(leg["expiry"]),
                        leg["strike"], leg["right"])
        for leg in legs
    ]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_builder_chain.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/chain.py backend/tests/test_builder_chain.py
git commit -m "feat(builder): read-only option chain + leg mids from IBKR"
```

---

### Task 6: Monitor single-tick (value + auto-close)

**Files:**
- Create: `backend/app/options/monitor.py`
- Test: `backend/tests/test_builder_monitor.py`

**Interfaces:**
- Consumes: `app.options.store`, `app.options.combo.combo_value`, `app.options.combo.exit_check`.
- Produces:
  - `async value_open_trades_once(mid_source) -> None` — `mid_source(underlying, legs) -> list[mid]` (async). For each open trade: value from mids; if `None`, skip; else persist `last_*` (dollars = `(value−net_debit)·100`) and paper-close on `exit_check`.
  - `async run_builder_monitor() -> None` — the app-lifespan loop (added in Task 7; not tested directly).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_monitor.py
import pytest

from app.db.base import init_db
from app.options import monitor, store

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0, "expiry": "2026-07-31", "qty": 1},
        {"action": "SELL", "right": "C", "strike": 105.0, "expiry": "2026-07-31", "qty": 1}]


def _mids(value):
    # a mid source that makes combo_value == `value` for the 2-leg (buy/sell) structure.
    async def src(underlying, legs):
        return [value, 0.0]   # BUY leg mid = value, SELL leg mid = 0 -> combo_value = value
    return src


@pytest.mark.asyncio
async def test_updates_pnl_then_hits_target():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)
    # value 2.1 -> +5%: updates, stays open.
    await monitor.value_open_trades_once(_mids(2.1))
    t = await store.get_trade(tid)
    assert t.status == "open" and t.last_pnl == pytest.approx(10.0)  # (2.1-2.0)*100
    # value 2.25 -> +12.5% >= target -> auto-close 'target'.
    await monitor.value_open_trades_once(_mids(2.25))
    t = await store.get_trade(tid)
    assert t.status == "closed" and t.exit_reason == "target"
    assert t.pnl == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_hits_stop():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)
    await monitor.value_open_trades_once(_mids(1.4))   # -30% <= -25% stop
    t = await store.get_trade(tid)
    assert t.status == "closed" and t.exit_reason == "stop"


@pytest.mark.asyncio
async def test_missing_mid_skips():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)

    async def missing(underlying, legs):
        return [None, 0.0]
    await monitor.value_open_trades_once(missing)
    t = await store.get_trade(tid)
    assert t.status == "open" and t.last_value is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_mon.db && DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db ../.venv/bin/python -m pytest tests/test_builder_monitor.py -q; rm -f _t_mon.db`
Expected: FAIL — `ModuleNotFoundError: app.options.monitor`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/options/monitor.py
"""Background monitor for builder (tracked paper) trades: revalue open trades from live mids,
persist P&L, and paper-close on target/stop. NEVER transmits orders."""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.ibkr.gateway import ConnState, get_gateway
from app.options import store
from app.options.chain import get_leg_mids
from app.options.combo import combo_value, exit_check
from app.services.journal import log_event
from app.services.market import get_market_status

logger = logging.getLogger(__name__)


async def value_open_trades_once(mid_source) -> None:
    """One valuation pass over all open trades. mid_source(underlying, legs) -> list[mid]."""
    for t in await store.list_open():
        try:
            mids = await mid_source(t.underlying, t.legs)
            value = combo_value(t.legs, mids)
            if value is None:
                continue  # a mid was unavailable this tick; leave prior valuation
            pnl = round((value - t.net_debit) * 100, 2)
            pnl_pct = (value - t.net_debit) / t.net_debit if t.net_debit else 0.0
            await store.update_valuation(t.id, value=round(value, 4), pnl=pnl,
                                         pnl_pct=round(pnl_pct, 6))
            reason = exit_check(t.net_debit, value, t.target_pct, t.stop_pct)
            if reason:
                await store.close_trade(t.id, pnl=pnl, reason=reason)
                await log_event(f"Builder trade {t.underlying} paper-closed ({reason}) "
                                f"P&L ${pnl:.2f}", category="trade")
        except Exception:  # noqa: BLE001 — never let one trade kill the pass
            logger.exception("builder valuation failed for trade %s", t.id)


async def run_builder_monitor() -> None:
    """App-lifespan loop. Only values when READY + regular market open (avoid stale-quote
    exits). Durable across restart — state lives in the table."""
    await log_event("Builder monitor started", category="action")
    while True:
        interval = get_settings().builder_monitor_interval_seconds
        try:
            if (get_gateway().state is ConnState.READY
                    and get_market_status().get("is_open")):
                await value_open_trades_once(get_leg_mids)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("builder monitor tick error")
        await asyncio.sleep(interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_mon.db && DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db ../.venv/bin/python -m pytest tests/test_builder_monitor.py -q; rm -f _t_mon.db`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/monitor.py backend/tests/test_builder_monitor.py
git commit -m "feat(builder): P&L monitor with target/stop paper-close (no orders)"
```

---

### Task 7: Config interval + start monitor in lifespan

**Files:**
- Modify: `backend/app/config.py` (add setting near `tc_manage_interval_seconds`, line ~75)
- Modify: `backend/app/main.py` (lifespan, after `_resume_active_workers()` at line ~53)
- Test: `backend/tests/test_builder_config.py`

**Interfaces:**
- Consumes: `run_builder_monitor` (Task 6), `get_settings`.
- Produces: `Settings.builder_monitor_interval_seconds: int = 60`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_config.py
def test_builder_interval_default():
    from app.config import get_settings
    assert get_settings().builder_monitor_interval_seconds == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_builder_config.py -q`
Expected: FAIL — `AttributeError: ... 'builder_monitor_interval_seconds'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/config.py`, directly below the line `tc_manage_interval_seconds: int = 300`:

```python
    builder_monitor_interval_seconds: int = 60  # options-builder paper-trade revaluation cadence
```

In `backend/app/main.py`, inside `lifespan`, change:

```python
    await _resume_active_workers()
    yield
```
to:
```python
    await _resume_active_workers()
    from app.options.monitor import run_builder_monitor
    asyncio.create_task(run_builder_monitor())
    yield
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_builder_config.py -q && ../.venv/bin/python -c "import app.main; print('main imports OK')"`
Expected: PASS (1 passed) + `main imports OK`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/main.py backend/tests/test_builder_config.py
git commit -m "feat(builder): run the paper-trade monitor as an app-lifespan task"
```

---

### Task 8: Builder API router

**Files:**
- Create: `backend/app/routers/builder.py`
- Modify: `backend/app/main.py` (import + `app.include_router(builder_router)` near line ~90-96)
- Test: `backend/tests/test_builder_api.py`

**Interfaces:**
- Consumes: `app.options.chain`, `app.options.store`, `app.options.combo`, `app.config.get_settings`.
- Produces router `prefix="/api/builder"` with:
  - `GET /chain?underlying=SYM` → `get_option_chain`.
  - `POST /quote` body `{underlying, legs}` → `{legs:[{...,mid}], net_debit, missing:[int]}`.
  - `POST /payoff` body `{legs, net_debit, iv, r?, eval_date?, lo?, hi?, points?}` → `{curve, max_profit, max_loss, breakevens}`.
  - `POST /trades` body `{underlying, legs, net_debit, iv, target_pct?, stop_pct?}` → saved trade dict.
  - `GET /trades?status=` → `{trades:[serialized...]}`.
  - `GET /trades/{id}` → serialized trade + `curve`.
  - `POST /trades/{id}/close` → manual paper close at `last_value` (or net_debit if unvalued).
- Produces `_serialize(rec) -> dict` (used by list/detail): includes `id, underlying, legs, net_debit, iv, target_pct, stop_pct, status, entry_time, exit_time, exit_reason, pnl, last_value, last_pnl, last_pnl_pct, valued_at`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_api.py
from datetime import date, timedelta

import pytest

from app.db.base import init_db
from app.routers import builder

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0,
         "expiry": (date.today() + timedelta(days=28)).isoformat(), "qty": 1},
        {"action": "SELL", "right": "C", "strike": 105.0,
         "expiry": (date.today() + timedelta(days=28)).isoformat(), "qty": 1}]


@pytest.mark.asyncio
async def test_payoff_and_save_and_list():
    await init_db()
    payoff = await builder.payoff(builder.PayoffBody(
        legs=LEGS, net_debit=2.0, iv=0.25, points=41))
    assert len(payoff["curve"]) == 41 and payoff["max_profit"] > 0

    saved = await builder.save_trade(builder.SaveBody(
        underlying="QQQ", legs=LEGS, net_debit=2.0, iv=0.25,
        target_pct=0.10, stop_pct=0.25))
    assert saved["status"] == "open" and saved["id"]

    listing = await builder.list_trades_route()
    assert any(t["id"] == saved["id"] for t in listing["trades"])

    closed = await builder.close_trade_route(saved["id"])
    assert closed["status"] == "closed" and closed["exit_reason"] == "manual"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_api.db && DATABASE_URL=sqlite+aiosqlite:///./_t_api.db ../.venv/bin/python -m pytest tests/test_builder_api.py -q; rm -f _t_api.db`
Expected: FAIL — `ModuleNotFoundError: app.routers.builder`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/routers/builder.py
"""Options Strategy Builder API. Read-only IBKR (chain + quotes); saved trades are tracked
(paper) only — NO orders are ever transmitted."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.options import chain as chain_mod
from app.options import store
from app.options.combo import combo_value, curve_stats, payoff_curve

router = APIRouter(prefix="/api/builder", tags=["builder"])


class Leg(BaseModel):
    action: str  # BUY | SELL
    right: str   # C | P
    strike: float
    expiry: str  # YYYY-MM-DD
    qty: int = 1


class QuoteBody(BaseModel):
    underlying: str
    legs: list[Leg]


class PayoffBody(BaseModel):
    legs: list[Leg]
    net_debit: float
    iv: float = 0.25
    r: float | None = None
    eval_date: str | None = None
    lo: float | None = None
    hi: float | None = None
    points: int = 61


class SaveBody(BaseModel):
    underlying: str
    legs: list[Leg]
    net_debit: float
    iv: float = 0.25
    target_pct: float | None = None
    stop_pct: float | None = None


def _legs(models: list[Leg]) -> list[dict]:
    return [m.model_dump() for m in models]


def _serialize(rec) -> dict:
    return {
        "id": rec.id, "underlying": rec.underlying, "legs": rec.legs,
        "net_debit": rec.net_debit, "iv": rec.iv,
        "target_pct": rec.target_pct, "stop_pct": rec.stop_pct,
        "status": rec.status,
        "entry_time": rec.entry_time.isoformat() if rec.entry_time else None,
        "exit_time": rec.exit_time.isoformat() if rec.exit_time else None,
        "exit_reason": rec.exit_reason, "pnl": rec.pnl,
        "last_value": rec.last_value, "last_pnl": rec.last_pnl,
        "last_pnl_pct": rec.last_pnl_pct,
        "valued_at": rec.valued_at.isoformat() if rec.valued_at else None,
    }


def _strike_span(legs: list[dict]) -> tuple[float, float]:
    strikes = [leg["strike"] for leg in legs] or [100.0]
    mid = sum(strikes) / len(strikes)
    return round(mid * 0.8, 2), round(mid * 1.2, 2)


@router.get("/chain")
async def chain(underlying: str) -> dict:
    return await chain_mod.get_option_chain(underlying)


@router.post("/quote")
async def quote(body: QuoteBody) -> dict:
    legs = _legs(body.legs)
    mids = await chain_mod.get_leg_mids(body.underlying, legs)
    out_legs = [{**leg, "mid": mid} for leg, mid in zip(legs, mids)]
    value = combo_value(legs, mids)
    missing = [i for i, m in enumerate(mids) if m is None]
    return {"legs": out_legs, "net_debit": None if value is None else round(value, 4),
            "missing": missing}


@router.post("/payoff")
async def payoff(body: PayoffBody) -> dict:
    legs = _legs(body.legs)
    r = body.r if body.r is not None else 0.04
    ev = date.fromisoformat(body.eval_date) if body.eval_date else min(
        date.fromisoformat(leg["expiry"]) for leg in legs)
    lo, hi = (body.lo, body.hi) if body.lo and body.hi else _strike_span(legs)
    curve = payoff_curve(legs, body.net_debit, ev, body.iv, r, lo, hi, body.points)
    return {"curve": curve, "eval_date": ev.isoformat(), **curve_stats(curve)}


@router.post("/trades")
async def save_trade(body: SaveBody) -> dict:
    tid = await store.save_trade(body.underlying, _legs(body.legs), body.net_debit,
                                 body.iv, body.target_pct, body.stop_pct)
    return _serialize(await store.get_trade(tid))


@router.get("/trades")
async def list_trades_route(status: str | None = None) -> dict:
    return {"trades": [_serialize(t) for t in await store.list_trades(status)]}


@router.get("/trades/{trade_id}")
async def get_trade_route(trade_id: int) -> dict:
    rec = await store.get_trade(trade_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown trade")
    ev = min(date.fromisoformat(leg["expiry"]) for leg in rec.legs)
    lo, hi = _strike_span(rec.legs)
    curve = payoff_curve(rec.legs, rec.net_debit, ev, rec.iv, 0.04, lo, hi, 61)
    return {**_serialize(rec), "curve": curve, "eval_date": ev.isoformat(),
            **curve_stats(curve)}


@router.post("/trades/{trade_id}/close")
async def close_trade_route(trade_id: int) -> dict:
    rec = await store.get_trade(trade_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown trade")
    pnl = rec.last_pnl if rec.last_pnl is not None else 0.0
    await store.close_trade(trade_id, pnl=pnl, reason="manual")
    return _serialize(await store.get_trade(trade_id))
```

Then in `backend/app/main.py`, add with the other router imports (near line 90):
```python
from app.routers.builder import router as builder_router
```
and with the other `include_router` calls:
```python
app.include_router(builder_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_api.db && DATABASE_URL=sqlite+aiosqlite:///./_t_api.db ../.venv/bin/python -m pytest tests/test_builder_api.py -q; rm -f _t_api.db`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/builder.py backend/app/main.py backend/tests/test_builder_api.py
git commit -m "feat(builder): /api/builder router (chain, quote, payoff, trades)"
```

---

### Task 9: Frontend API client methods

**Files:**
- Modify: `frontend/src/api.js` (add to the `api` object, after the `optionsPosition` line)

**Interfaces:**
- Produces on `api`: `builderChain(underlying)`, `builderQuote(underlying, legs)`, `builderPayoff(body)`, `builderSaveTrade(body)`, `builderTrades(status?)`, `builderTrade(id)`, `builderCloseTrade(id)`.

- [ ] **Step 1: Add the methods**

In `frontend/src/api.js`, immediately after the `optionsPosition:` line, insert:

```javascript
  // Options Strategy Builder (tracked paper trades — no orders transmitted).
  builderChain: (underlying) => request(`/api/builder/chain?underlying=${encodeURIComponent(underlying)}`),
  builderQuote: (underlying, legs) =>
    request(`/api/builder/quote`, { method: "POST", body: JSON.stringify({ underlying, legs }) }),
  builderPayoff: (body) =>
    request(`/api/builder/payoff`, { method: "POST", body: JSON.stringify(body) }),
  builderSaveTrade: (body) =>
    request(`/api/builder/trades`, { method: "POST", body: JSON.stringify(body) }),
  builderTrades: (status) =>
    request(`/api/builder/trades${status ? `?status=${status}` : ""}`),
  builderTrade: (id) => request(`/api/builder/trades/${id}`),
  builderCloseTrade: (id) =>
    request(`/api/builder/trades/${id}/close`, { method: "POST" }),
```

- [ ] **Step 2: Verify the build still compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...` (no error).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api.js
git commit -m "feat(builder): frontend API client methods"
```

---

### Task 10: PayoffChart component

**Files:**
- Create: `frontend/src/components/PayoffChart.jsx`

**Interfaces:**
- Consumes: `recharts`.
- Produces: default export `PayoffChart({ curve, breakevens })` — an area/line P&L-vs-price chart with a zero reference line; green above zero, red below. `curve` is `[{price, pnl}]`.

- [ ] **Step 1: Create the component**

```jsx
// frontend/src/components/PayoffChart.jsx
import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

// Payoff diagram: P&L (dollars) across underlying price at the chosen evaluation date.
export default function PayoffChart({ curve, breakevens = [] }) {
  if (!curve || curve.length === 0) return <div className="muted">No payoff data.</div>;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={curve} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="pnlPos" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#34d399" stopOpacity={0.5} />
            <stop offset="100%" stopColor="#34d399" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#243" />
        <XAxis dataKey="price" tick={{ fontSize: 10 }} minTickGap={40} />
        <YAxis tick={{ fontSize: 10 }} />
        <Tooltip
          contentStyle={{ background: "#131722", border: "1px solid #242c3a", borderRadius: 8 }}
          formatter={(v) => [`$${v}`, "P&L"]}
          labelFormatter={(l) => `Underlying ${l}`}
        />
        <ReferenceLine y={0} stroke="#5c6677" />
        {breakevens.map((be) => (
          <ReferenceLine key={be} x={be} stroke="#818cf8" strokeDasharray="4 4" />
        ))}
        <Area type="monotone" dataKey="pnl" stroke="#34d399" fill="url(#pnlPos)" />
      </AreaChart>
    </ResponsiveContainer>
  );
}
```

- [ ] **Step 2: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/PayoffChart.jsx
git commit -m "feat(builder): payoff diagram chart component"
```

---

### Task 11: BuilderTab component

**Files:**
- Create: `frontend/src/components/BuilderTab.jsx`

**Interfaces:**
- Consumes: `api` (Task 9 methods), `PayoffChart` (Task 10).
- Produces: default export `BuilderTab()` — the full build → preview → save → tracked-list UI.

- [ ] **Step 1: Create the component**

```jsx
// frontend/src/components/BuilderTab.jsx
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import PayoffChart from "./PayoffChart.jsx";

const money = (n) => (n == null ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const emptyLeg = (expiry, strike) => ({ action: "BUY", right: "C", strike, expiry, qty: 1 });

export default function BuilderTab() {
  const [underlying, setUnderlying] = useState("QQQ");
  const [chain, setChain] = useState({ expirations: [], strikes: [] });
  const [legs, setLegs] = useState([]);
  const [netDebit, setNetDebit] = useState(null);
  const [iv, setIv] = useState(0.25);
  const [payoff, setPayoff] = useState(null);
  const [target, setTarget] = useState(10);
  const [stop, setStop] = useState(25);
  const [trades, setTrades] = useState([]);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const loadChain = () => api.builderChain(underlying)
    .then((c) => { setChain(c); }).catch((e) => setMsg(e.message));
  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades)).catch(() => {});

  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);

  const addLeg = () => setLegs((ls) => [...ls, emptyLeg(chain.expirations[0] || "", chain.strikes[Math.floor(chain.strikes.length / 2)] || 0)]);
  const setLeg = (i, patch) => setLegs((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const removeLeg = (i) => setLegs((ls) => ls.filter((_, j) => j !== i));

  const fetchQuotes = async () => {
    setBusy(true); setMsg(null);
    try {
      const q = await api.builderQuote(underlying, legs);
      setNetDebit(q.net_debit);
      setLegs((ls) => ls.map((l, i) => ({ ...l, mid: q.legs[i]?.mid })));
      if (q.missing?.length) setMsg(`No quote for ${q.missing.length} leg(s).`);
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const preview = async () => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try { setPayoff(await api.builderPayoff({ legs, net_debit: netDebit, iv: Number(iv) })); }
    catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const save = async () => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try {
      await api.builderSaveTrade({
        underlying, legs, net_debit: netDebit, iv: Number(iv),
        target_pct: target === "" ? null : Number(target) / 100,
        stop_pct: stop === "" ? null : Number(stop) / 100,
      });
      setMsg("Saved as a tracked paper trade.");
      loadTrades();
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const closeTrade = async (id) => { await api.builderCloseTrade(id).catch(() => {}); loadTrades(); };

  const debitLabel = useMemo(() => netDebit == null ? "—"
    : netDebit >= 0 ? `$${netDebit.toFixed(2)} debit` : `$${Math.abs(netDebit).toFixed(2)} credit`, [netDebit]);

  return (
    <div className="builder-tab">
      <h1>Options Builder</h1>
      <p className="muted small">Build a multi-leg structure, preview its payoff, and save it as a
        tracked <b>paper</b> trade. Nothing is transmitted to the broker.</p>

      <section className="card">
        <div className="row gap center">
          <input className="input narrow" value={underlying}
            onChange={(e) => setUnderlying(e.target.value.toUpperCase())} placeholder="Underlying" />
          <button className="btn" onClick={loadChain}>Load chain</button>
          <span className="muted small">{chain.expirations.length} expiries · {chain.strikes.length} strikes</span>
        </div>

        <h4>Legs</h4>
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Side</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Mid</th><th></th></tr></thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i}>
                  <td><select value={l.action} onChange={(e) => setLeg(i, { action: e.target.value })}><option>BUY</option><option>SELL</option></select></td>
                  <td><select value={l.right} onChange={(e) => setLeg(i, { right: e.target.value })}><option value="C">Call</option><option value="P">Put</option></select></td>
                  <td><select value={l.strike} onChange={(e) => setLeg(i, { strike: Number(e.target.value) })}>{chain.strikes.map((s) => <option key={s} value={s}>{s}</option>)}</select></td>
                  <td><select value={l.expiry} onChange={(e) => setLeg(i, { expiry: e.target.value })}>{chain.expirations.map((x) => <option key={x} value={x}>{x}</option>)}</select></td>
                  <td><input className="input tiny" type="number" min="1" value={l.qty} onChange={(e) => setLeg(i, { qty: Number(e.target.value) })} /></td>
                  <td className="num">{l.mid == null ? "—" : l.mid.toFixed(2)}</td>
                  <td><button className="btn ghost tiny" onClick={() => removeLeg(i)}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="row gap center">
          <button className="btn ghost" onClick={addLeg} disabled={!chain.expirations.length}>+ Add leg</button>
          <button className="btn" onClick={fetchQuotes} disabled={busy || !legs.length}>Fetch quotes</button>
          <span className="muted">Net: <b>{debitLabel}</b></span>
        </div>
      </section>

      <section className="card">
        <div className="row between center">
          <h2>Payoff</h2>
          <div className="row gap center">
            <label className="muted small">IV</label>
            <input className="input tiny" type="number" step="0.01" value={iv} onChange={(e) => setIv(e.target.value)} />
            <button className="btn" onClick={preview} disabled={busy || !legs.length}>Preview</button>
          </div>
        </div>
        {payoff ? (
          <>
            <PayoffChart curve={payoff.curve} breakevens={payoff.breakevens} />
            <div className="muted small">At {payoff.eval_date} · max profit {money(payoff.max_profit)} · max loss {money(payoff.max_loss)} · breakevens {payoff.breakevens.join(", ") || "—"}</div>
          </>
        ) : <div className="muted">Fetch quotes, then Preview to see the payoff.</div>}
      </section>

      <section className="card">
        <h2>Save as tracked trade</h2>
        <div className="row gap center">
          <label className="muted small">Target % of debit</label>
          <input className="input tiny" type="number" value={target} onChange={(e) => setTarget(e.target.value)} />
          <label className="muted small">Stop %</label>
          <input className="input tiny" type="number" value={stop} onChange={(e) => setStop(e.target.value)} />
          <button className="btn primary" onClick={save} disabled={busy || netDebit == null}>Save trade</button>
        </div>
        {msg && <div className="banner ok">{msg}</div>}
      </section>

      <section className="card">
        <h2>Tracked trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Underlying</th><th>Legs</th><th>Net debit</th><th>P&L</th><th>Status</th><th></th></tr></thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id}>
                    <td>{t.underlying}</td>
                    <td className="muted small">{t.legs.length} legs</td>
                    <td className="num">{t.net_debit >= 0 ? `$${t.net_debit.toFixed(2)}` : `($${Math.abs(t.net_debit).toFixed(2)})`}</td>
                    <td className={`num ${(t.status === "closed" ? t.pnl : t.last_pnl) >= 0 ? "pos" : "neg"}`}>
                      {money(t.status === "closed" ? t.pnl : t.last_pnl)}
                      {t.last_pnl_pct != null && t.status === "open" && <span className="muted small"> {(t.last_pnl_pct * 100).toFixed(1)}%</span>}
                    </td>
                    <td><span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>{t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span></td>
                    <td>{t.status === "open" && <button className="btn ghost tiny" onClick={() => closeTrade(t.id)}>Close</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/BuilderTab.jsx
git commit -m "feat(builder): Builder tab UI (build, payoff, save, tracked list)"
```

---

### Task 12: Wire the tab into the app + styles

**Files:**
- Modify: `frontend/src/App.jsx` (import, `TABS`, `Icon` paths, render switch)
- Modify: `frontend/src/styles.css` (append builder styles + reuse; add `.input.tiny`, `.btn.tiny`, `.builder-tab`)

**Interfaces:**
- Consumes: `BuilderTab` (Task 11).

- [ ] **Step 1: Wire App.jsx**

In `frontend/src/App.jsx`:
- Add import after the `ApiTab` import:
```javascript
import BuilderTab from "./components/BuilderTab.jsx";
```
- Add a `Builder` icon path inside the `paths` object in `Icon`:
```javascript
    Builder: "M4 4h16v4H4V4zm0 6h10v4H4v-4zm0 6h16v4H4v-4z",
```
- Change `TABS`:
```javascript
const TABS = ["Dashboard", "Strategies", "Builder", "Journal", "API"];
```
- Add the render branch alongside the others in `<main>`:
```javascript
        {tab === "Builder" && <BuilderTab />}
```

- [ ] **Step 2: Append styles**

Append to `frontend/src/styles.css`:

```css
/* ===================== options builder ===================== */
.builder-tab h1 { margin-bottom: 6px; }
.builder-tab .card { margin-bottom: 16px; }
.input.tiny { width: 72px; padding: 5px 8px; font-size: 13px; }
.btn.tiny { padding: 3px 8px; font-size: 11px; }
.builder-tab select {
  background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 8px; font-size: 13px;
}
.builder-tab .table td { padding: 8px 10px; }
```

- [ ] **Step 3: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.jsx frontend/src/styles.css
git commit -m "feat(builder): add Builder tab to the sidebar + styles"
```

---

### Task 13: Full regression + manual verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full builder + core backend test suite**

Run:
```bash
cd backend && rm -f _t_all.db
DATABASE_URL=sqlite+aiosqlite:///./_t_all.db ../.venv/bin/python -m pytest \
  tests/test_options_bs.py tests/test_options_combo.py tests/test_builder_model.py \
  tests/test_builder_store.py tests/test_builder_chain.py tests/test_builder_monitor.py \
  tests/test_builder_config.py tests/test_builder_api.py -q
rm -f _t_all.db
../.venv/bin/python -m tests.test_kill_switch | tail -1
```
Expected: all builder tests pass; kill switch still passes.

- [ ] **Step 2: Rebuild frontend + restart app + smoke the endpoints**

Run:
```bash
cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep "built in"
cd .. && launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
for i in $(seq 1 30); do curl -s -m2 http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 1; done
curl -s http://127.0.0.1:8765/api/builder/trades | python3 -m json.tool
```
Expected: health up; `{"trades": []}` (or existing trades). No error.

- [ ] **Step 3: Manual — build + save + verify tracking**

In the running app (`http://127.0.0.1:8765`): open **Builder**, load a chain (QQQ), add two call legs at different expiries (a calendar), Fetch quotes → net debit shows; Preview → payoff "tent" renders; set target 10 / stop 25 → Save → the trade appears in **Tracked trades** with a P&L cell. Confirm no order-related log lines appear (search logs for `[LIVE COMBO]` / `[PAPER] COMBO` — there should be none from the builder).

- [ ] **Step 4: Final commit (if any manual-fix tweaks were needed)**

```bash
git add -A && git commit -m "test(builder): verify end-to-end tracking (no orders)" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** chain dropdowns (T5/T8/T11), BS payoff w/ manual IV (T1/T2/T8/T10/T11), save-no-transmit (T4/T8), auto-update + target/stop paper-close (T6/T7), debit-only exit (T2/T6), new table no-migration (T3), READY+RTH gate + restart-durable monitor (T6/T7), tracked-list UI + polling (T11), tests per module (T1–T8), manual verify (T13). All covered.
- **No-order invariant** enforced by never importing `submit_combo_order`; T13 step 3 checks logs.
- **Types consistent:** canonical leg dict, `combo_value(legs, mids)` list form, `_serialize` field names match `BuilderPositionRecord` columns.
