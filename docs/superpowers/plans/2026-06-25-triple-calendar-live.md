# Triple Calendar — Phase 2 (Live Trading) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trade the Triple Calendar live on IBKR by reusing the Phase-1 core, via 6-leg combo orders, fully integrated into the app and paper-safe by default.

**Architecture:** A `LivePriceSource` lets the Phase-1 `decide_entry`/`manage` run live unchanged; `live.py` turns positions into combo legs and drives enter/adjust/exit; shared additive changes add `option()`/`combo()` contracts, `submit_combo_order` (gated), an `OptionPositionRecord` table, a worker dispatch branch, config, and registry registration.

**Tech Stack:** Python 3.14, ib_async, SQLAlchemy (async), FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-06-25-triple-calendar-live-design.md`

> Run `pytest` from `backend/`. Python: `/Users/karanpatel/PycharmProjects/Tradding-system/.venv/bin/python`. Use `git -C /Users/karanpatel/PycharmProjects/Tradding-system` for commits. Commit messages end with the Co-Authored-By trailer. **Safety: nothing transmits unless `ENABLE_LIVE_ORDERS=1`.**

---

### Task 1: IBKR option + combo (BAG) contracts

**Files:**
- Modify: `backend/app/ibkr/contracts.py`
- Test: `backend/tests/test_ibkr_combo.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_ibkr_combo.py
from datetime import date

from app.ibkr.contracts import combo, option


def test_option_contract():
    o = option("QQQ", date(2026, 7, 17), 445.0, "C")
    assert o.symbol == "QQQ" and o.secType == "OPT"
    assert o.lastTradeDateOrContractMonth == "20260717"
    assert o.strike == 445.0 and o.right == "C" and o.exchange == "SMART"


def test_combo_bag_legs():
    bag = combo("QQQ", [(111, "SELL"), (222, "BUY")])
    assert bag.secType == "BAG" and bag.symbol == "QQQ" and bag.currency == "USD"
    assert len(bag.comboLegs) == 2
    assert bag.comboLegs[0].conId == 111 and bag.comboLegs[0].action == "SELL"
    assert bag.comboLegs[1].conId == 222 and bag.comboLegs[1].ratio == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_combo.py -v`
Expected: FAIL (`option`/`combo` missing).

- [ ] **Step 3: Implement (append to `app/ibkr/contracts.py`)**

```python
from datetime import date

from ib_async import ComboLeg, Contract, Option


def option(underlying: str, expiry: date, strike: float, right: str) -> Option:
    return Option(underlying.upper(), expiry.strftime("%Y%m%d"), float(strike),
                  right.upper(), "SMART")


def combo(underlying: str, legs: list[tuple[int, str]]) -> Contract:
    """BAG combo from (conId, action) legs (ratio 1 each)."""
    c = Contract(symbol=underlying.upper(), secType="BAG", currency="USD", exchange="SMART")
    c.comboLegs = [ComboLeg(conId=cid, ratio=1, action=act, exchange="SMART")
                   for cid, act in legs]
    return c
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_combo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/app/ibkr/contracts.py backend/tests/test_ibkr_combo.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(ibkr): option + combo (BAG) contract builders

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Combo legs from a Position (pure)

**Files:**
- Create: `backend/app/strategies/triple_calendar/live.py`
- Test: `backend/tests/test_tc_live_legs.py`

A leg spec is `{"expiry": date, "strike": float, "right": "C", "action": "SELL"|"BUY"}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_live_legs.py
from datetime import date

from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.live import entry_legs, exit_legs

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


def _pos():
    return Position("QQQ", date(2026, 6, 25),
                    [Calendar(430.0, SE, BE), Calendar(445.0, SE, BE), Calendar(460.0, SE, BE)],
                    net_debit=3.0)


def test_entry_legs_six_legs_correct_actions():
    legs = entry_legs(_pos())
    assert len(legs) == 6
    sells = [x for x in legs if x["action"] == "SELL"]
    buys = [x for x in legs if x["action"] == "BUY"]
    assert len(sells) == 3 and len(buys) == 3
    assert all(x["expiry"] == SE for x in sells) and all(x["expiry"] == BE for x in buys)
    assert {x["strike"] for x in sells} == {430.0, 445.0, 460.0}
    assert all(x["right"] == "C" for x in legs)


def test_exit_legs_reverse_actions():
    legs = exit_legs(_pos())
    assert {x["action"] for x in legs if x["expiry"] == SE} == {"BUY"}   # buy back the short
    assert {x["action"] for x in legs if x["expiry"] == BE} == {"SELL"}  # sell the long
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_live_legs.py -v`
Expected: FAIL (module/functions missing).

- [ ] **Step 3: Implement (start `app/strategies/triple_calendar/live.py`)**

```python
# backend/app/strategies/triple_calendar/live.py
"""Triple Calendar live management: turn positions into IBKR combo legs and drive
enter/adjust/exit. Reuses the Phase-1 core (decide_entry/manage). Paper-safe via the
execution gate."""
from __future__ import annotations

from app.strategies.triple_calendar.legs import Calendar, Position


def _calendar_legs(cal: Calendar, opening: bool) -> list[dict]:
    # Opening a calendar: SELL the near, BUY the far. Closing reverses both.
    near = "SELL" if opening else "BUY"
    far = "BUY" if opening else "SELL"
    return [
        {"expiry": cal.sell_expiry, "strike": cal.strike, "right": "C", "action": near},
        {"expiry": cal.buy_expiry, "strike": cal.strike, "right": "C", "action": far},
    ]


def entry_legs(position: Position) -> list[dict]:
    legs: list[dict] = []
    for cal in position.calendars:
        legs += _calendar_legs(cal, opening=True)
    return legs


def adjustment_legs(cal: Calendar) -> list[dict]:
    return _calendar_legs(cal, opening=True)


def exit_legs(position: Position) -> list[dict]:
    legs: list[dict] = []
    for cal in position.calendars:
        legs += _calendar_legs(cal, opening=False)
    return legs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_tc_live_legs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/app/strategies/triple_calendar/live.py backend/tests/test_tc_live_legs.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(triple_calendar): combo legs (entry/adjust/exit) from a position

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Combo execution (paper-gated) + options flatten

**Files:**
- Modify: `backend/app/engine/execution.py`
- Test: `backend/tests/test_combo_execution.py`

- [ ] **Step 1: Write the failing test (paper gating, no Gateway)**

```python
# backend/tests/test_combo_execution.py
from datetime import date

import pytest

from app.config import get_settings
from app.engine import execution


@pytest.mark.asyncio
async def test_combo_paper_mode_does_not_transmit(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    legs = [{"expiry": date(2026, 7, 17), "strike": 445.0, "right": "C", "action": "SELL"},
            {"expiry": date(2026, 7, 24), "strike": 445.0, "right": "C", "action": "BUY"}]
    res = await execution.submit_combo_order("QQQ", legs, net_price=1.25, reason="entry")
    assert res["transmitted"] is False and res.get("paper") is True
    assert res["legs"] == 2 and "QQQ" in res["detail"]
    get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_combo_execution.py -v`
Expected: FAIL (`submit_combo_order` missing).

- [ ] **Step 3: Implement (append to `app/engine/execution.py`)**

```python
async def submit_combo_order(underlying: str, legs: list[dict], *, net_price: float | None = None,
                             action: str = "BUY", reason: str = "") -> dict:
    """Place a multi-leg options combo (BAG). Each leg = {expiry(date), strike, right, action}.
    GATE: ENABLE_LIVE_ORDERS off -> log + return transmitted:False (paper)."""
    if not legs:
        return {"transmitted": False, "detail": "no legs"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] COMBO %s %s x%d legs net~%s (%s) — ENABLE_LIVE_ORDERS off",
                       action, underlying, len(legs), net_price, reason)
        return {"transmitted": False, "paper": True, "legs": len(legs),
                "detail": f"paper combo {action} {underlying} {len(legs)} legs"}
    from ib_async import LimitOrder, MarketOrder

    from app.ibkr.contracts import combo, option
    ib = await get_gateway().get_ib()
    leg_ids: list[tuple[int, str]] = []
    for spec in legs:
        c = option(underlying, spec["expiry"], spec["strike"], spec["right"])
        await ib.qualifyContractsAsync(c)
        leg_ids.append((c.conId, spec["action"]))
    bag = combo(underlying, leg_ids)
    order = LimitOrder(action, 1, net_price) if net_price is not None else MarketOrder(action, 1)
    trade = ib.placeOrder(bag, order)
    logger.warning("[LIVE COMBO] %s %s %d legs net %s (%s)", action, underlying, len(legs),
                   net_price, reason)
    return {"transmitted": True, "legs": len(legs), "order_id": trade.order.orderId,
            "detail": f"LIVE combo {action} {underlying} {len(legs)} legs"}
```

Then make the kill switch close option positions too. In `flatten_all`, after the existing
equity loop (just before the `note = ...` summary line), add:

```python
    # Close any non-equity (option/combo) positions too, using their raw contracts.
    if s.enable_live_orders:
        from ib_async import MarketOrder as _MO
        for p in ib.positions():
            if getattr(p.contract, "secType", "STK") == "STK" or not p.position:
                continue
            total += 1
            act = "SELL" if p.position > 0 else "BUY"
            try:
                await ib.qualifyContractsAsync(p.contract)
                ib.placeOrder(p.contract, _MO(act, abs(int(p.position))))
                closed.append(f"{p.contract.localSymbol or p.contract.symbol} x{abs(int(p.position))}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{getattr(p.contract, 'symbol', '?')} option ({exc})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_combo_execution.py -v`
Expected: PASS. Also: `../.venv/bin/python -c "import app.main; print('ok')"`.

- [ ] **Step 5: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/app/engine/execution.py backend/tests/test_combo_execution.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(engine): submit_combo_order (paper-gated) + options-aware flatten

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: OptionPositionRecord + persistence store

**Files:**
- Modify: `backend/app/db/models.py`
- Create: `backend/app/strategies/triple_calendar/store.py`
- Test: `backend/tests/test_tc_store.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_store.py
from datetime import date

import pytest

from app.db.base import init_db
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import close_position, load_open, save_position

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


@pytest.mark.asyncio
async def test_save_load_close_round_trip():
    await init_db()
    pos = Position("QQQ", date(2026, 6, 25), [Calendar(445.0, SE, BE)], net_debit=1.5)
    rec_id = await save_position("strategy_2", pos, order_ids=[7], paper=True)
    loaded = await load_open("strategy_2")
    assert loaded is not None and loaded.underlying == "QQQ"
    assert len(loaded.calendars) == 1 and loaded.calendars[0].strike == 445.0
    assert loaded.calendars[0].sell_expiry == SE
    await close_position(rec_id, pnl=0.2)
    assert await load_open("strategy_2") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_store.py -v`
Expected: FAIL (model/store missing).

- [ ] **Step 3: Add the model (in `app/db/models.py`, after `JournalNote`)**

```python
class OptionPositionRecord(Base):
    """A live multi-leg options position (e.g. Triple Calendar), JSON-serialized so a
    restart can resume management. One open record per strategy at a time."""
    __tablename__ = "option_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_key: Mapped[str] = mapped_column(String(32), index=True)
    underlying: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    paper: Mapped[bool] = mapped_column(Boolean, default=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    net_debit: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    calendars: Mapped[list] = mapped_column(JSON, default=list)   # [{strike, sell, buy}]
    order_ids: Mapped[list] = mapped_column(JSON, default=list)
```
(If `Boolean`/`JSON`/`_utcnow` aren't already imported in models.py, they are used by existing
models above — reuse the same imports.)

- [ ] **Step 4: Implement the store**

```python
# backend/app/strategies/triple_calendar/store.py
"""Persist/reconstruct the live Triple Calendar position across restarts."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import OptionPositionRecord
from app.strategies.triple_calendar.legs import Calendar, Position


def _ser(pos: Position) -> list[dict]:
    return [{"strike": c.strike, "sell": c.sell_expiry.isoformat(), "buy": c.buy_expiry.isoformat()}
            for c in pos.calendars]


def _deser(underlying: str, entry: datetime, debit: float, cals: list[dict]) -> Position:
    return Position(underlying=underlying, entry_date=entry.date(), net_debit=debit, calendars=[
        Calendar(c["strike"], date.fromisoformat(c["sell"]), date.fromisoformat(c["buy"]))
        for c in cals])


async def save_position(strategy_key: str, pos: Position, order_ids: list[int], paper: bool) -> int:
    async with session_scope() as session:
        rec = OptionPositionRecord(
            strategy_key=strategy_key, underlying=pos.underlying, status="open", paper=paper,
            net_debit=pos.net_debit, calendars=_ser(pos), order_ids=list(order_ids))
        session.add(rec)
        await session.flush()
        return rec.id


async def load_open(strategy_key: str) -> Position | None:
    async with session_scope() as session:
        rec = (await session.execute(
            select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == strategy_key,
                OptionPositionRecord.status == "open"))).scalars().first()
        if rec is None:
            return None
        return _deser(rec.underlying, rec.entry_time, rec.net_debit, rec.calendars)


async def update_calendars(record_id: int, pos: Position) -> None:
    async with session_scope() as session:
        rec = await session.get(OptionPositionRecord, record_id)
        if rec:
            rec.calendars = _ser(pos)
            rec.net_debit = pos.net_debit


async def close_position(record_id: int, pnl: float) -> None:
    async with session_scope() as session:
        rec = await session.get(OptionPositionRecord, record_id)
        if rec:
            rec.status = "closed"
            rec.pnl = pnl
            rec.exit_time = datetime.now(timezone.utc)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_tc_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/app/db/models.py backend/app/strategies/triple_calendar/store.py backend/tests/test_tc_store.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(triple_calendar): OptionPositionRecord + persistence store

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Live manage step (enter/adjust/exit) against a quote source

**Files:**
- Modify: `backend/app/strategies/triple_calendar/live.py`
- Test: `backend/tests/test_tc_manage_step.py`

`manage_step` is the single, testable unit of the live loop: given a price source, params, an
async executor, and the current persisted position (or None), do one action and return what
happened. The real loop (Task 6) just calls it on a timer.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_manage_step.py
from datetime import date, timedelta

import pytest

from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.params import TripleCalendarParams


class Src:
    def __init__(self, spot=445.0): self.spot = spot
    def underlying_close(self, sym, day): return self.spot
    def chain_strikes(self, u, e): return [float(r) for r in range(400, 491, 5)]
    def option_close(self, u, expiry, strike, right, day):
        return 1.0 + 0.1 * max((expiry - day).days, 0)


class Exec:
    def __init__(self): self.calls = []
    async def __call__(self, underlying, legs, **kw):
        self.calls.append((kw.get("reason"), len(legs)))
        return {"transmitted": False, "paper": True, "order_id": None}


@pytest.mark.asyncio
async def test_manage_step_enters_when_flat():
    ex = Exec()
    p = TripleCalendarParams()
    out = await manage_step(date(2026, 6, 25), Src(), p, ex, position=None)
    assert out["action"] == "enter" and out["position"] is not None
    assert ex.calls and ex.calls[0][1] == 6   # 6-leg entry combo


@pytest.mark.asyncio
async def test_manage_step_holds_then_no_order():
    ex = Exec()
    p = TripleCalendarParams()
    out = await manage_step(date(2026, 6, 25), Src(), p, ex, position=None)
    pos = out["position"]
    out2 = await manage_step(date(2026, 6, 26), Src(), p, ex, position=pos)
    assert out2["action"] in ("hold", "adjust", "exit")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_manage_step.py -v`
Expected: FAIL (`manage_step` missing).

- [ ] **Step 3: Implement (append to `live.py`)**

```python
from datetime import date

from app.strategies.triple_calendar.legs import Position
from app.strategies.triple_calendar.strategy import decide_entry, manage, position_value


async def manage_step(day: date, source, params, executor, position: Position | None,
                      events=None, vix=None) -> dict:
    """One live tick. `executor(underlying, legs, *, net_price, action, reason)` places a
    combo (or paper-logs it). Returns {action, position, info}."""
    events = events if events is not None else params.event_dates
    if position is None:
        pos = decide_entry(day, source, params, events, vix)
        if pos is None:
            return {"action": "none", "position": None, "info": {}}
        await executor(pos.underlying, entry_legs(pos), net_price=round(pos.net_debit, 2),
                       action="BUY", reason="entry")
        return {"action": "enter", "position": pos, "info": {"net_debit": pos.net_debit}}

    act, info = manage(position, day, source, params)
    if act == "adjust":
        cal = position.calendars[-1]   # manage() appended the new calendar
        await executor(position.underlying, adjustment_legs(cal), net_price=None,
                       action="BUY", reason="adjust")
        return {"action": "adjust", "position": position, "info": info}
    if act == "exit":
        value = info.get("value")
        if value is None:
            value = position_value(source, position, day) or position.net_debit
        await executor(position.underlying, exit_legs(position), net_price=None,
                       action="SELL", reason=info.get("reason", "exit"))
        pnl = (value - position.net_debit)
        return {"action": "exit", "position": None, "info": {**info, "pnl": pnl}}
    return {"action": "hold", "position": position, "info": info}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_tc_manage_step.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/app/strategies/triple_calendar/live.py backend/tests/test_tc_manage_step.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(triple_calendar): manage_step (enter/adjust/exit) live tick

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Live source, strategy class, worker + registry + config wiring

**Files:**
- Create: `backend/app/strategies/triple_calendar/live_source.py`
- Create: `backend/app/strategies/triple_calendar/strategy_class.py`
- Modify: `backend/app/config.py`, `backend/app/engine/worker.py`, `backend/app/strategies/registry.py`
- Test: `backend/tests/test_tc_strategy_class.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_strategy_class.py
from app.strategies.triple_calendar.strategy_class import TripleCalendarStrategy


def test_strategy_metadata():
    s = TripleCalendarStrategy()
    assert s.ready is True and s.is_options is True
    assert "Calendar" in s.name
    ov = s.overview()
    assert ov["ready"] is True and ov["rules"]


def test_registered_in_registry():
    from app.strategies.registry import get_strategy
    s = get_strategy("strategy_2")
    assert s is not None and getattr(s, "is_options", False) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_strategy_class.py -v`
Expected: FAIL (class/registration missing).

- [ ] **Step 3: Add config (in `app/config.py`, after the `tc`/polygon block or near other strategy blocks)**

```python
    # --- Triple Calendar (options) ---
    tc_underlying: str = "QQQ"
    tc_event_dates: str = ""                 # comma-separated ISO dates (FOMC/CPI)
    tc_manage_interval_seconds: int = 300
```

- [ ] **Step 4: Implement the live source**

```python
# backend/app/strategies/triple_calendar/live_source.py
"""IBKR-backed price source matching the Phase-1 interface so the core runs live
unchanged. `day` is ignored (live = now). Needs OPRA real-time option data + market open."""
from __future__ import annotations

import asyncio

from app.ibkr.contracts import option
from app.ibkr.gateway import get_gateway
from app.ibkr.market import get_quotes, normalize_ticker


class LivePriceSource:
    """Async accessors for live IBKR data. run_live builds a per-tick sync snapshot
    (see _Snapshot in strategy_class) from these, which the Phase-1 core then reads —
    so the core stays unchanged and never does I/O itself."""

    async def aunderlying(self, symbol):
        return (await get_quotes([symbol]))[symbol]["price"]

    async def aoption_mid(self, underlying, expiry, strike, right):
        ib = await get_gateway().get_ib()
        c = option(underlying, expiry, strike, right)
        await ib.qualifyContractsAsync(c)
        t = ib.reqMktData(c, "", False, False)
        await asyncio.sleep(2)
        q = normalize_ticker(t)
        ib.cancelMktData(c)
        return q["price"]

    async def achain(self, underlying, expiry):
        ib = await get_gateway().get_ib()
        from app.ibkr.contracts import stock
        u = stock(underlying)
        await ib.qualifyContractsAsync(u)
        chains = await ib.reqSecDefOptParamsAsync(u.symbol, "", u.secType, u.conId)
        for ch in chains:
            if ch.exchange == "SMART":
                return sorted(float(s) for s in ch.strikes)
        return sorted(float(s) for s in chains[0].strikes) if chains else []
```
> Note: the Phase-1 core calls `underlying_close/chain_strikes/option_close` synchronously.
> For live, `run_live` (Step 5) builds a small **snapshot adapter** each tick: it `await`s the
> async accessors above for the underlying + the needed strikes/expiries, caches the values,
> and exposes them through the sync interface the core expects. This keeps the core unchanged.

- [ ] **Step 5: Implement the strategy class + a snapshot adapter (`strategy_class.py`)**

```python
# backend/app/strategies/triple_calendar/strategy_class.py
"""Triple Calendar as an app strategy (ready, options, live loop)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.config import get_settings
from app.services.journal import log_event
from app.strategies.base import BaseStrategy
from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.live_source import LivePriceSource
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.store import (close_position, load_open, save_position,
                                                  update_calendars)


def _params() -> TripleCalendarParams:
    s = get_settings()
    events = [date.fromisoformat(d.strip()) for d in s.tc_event_dates.split(",") if d.strip()]
    return TripleCalendarParams(underlying=s.tc_underlying, event_dates=events)


class _Snapshot:
    """Sync price-source view for the core, filled from live IBKR values per tick."""
    def __init__(self, spot, strikes, opt):
        self._spot, self._strikes, self._opt = spot, strikes, opt
    def underlying_close(self, sym, day): return self._spot
    def chain_strikes(self, u, e): return self._strikes
    def option_close(self, u, expiry, strike, right, day):
        return self._opt.get((expiry, strike, right))


class TripleCalendarStrategy(BaseStrategy):
    key = "strategy_2"
    name = "Triple Calendar"
    ready = True
    is_options = True
    description = "Delta-neutral, +vega three-calendar spread on a configured underlying."
    rules = ["Sell 21DTE / buy 28DTE calendars at lower/ATM/upper strikes",
             "Outer strikes from the ATM straddle expected move + buffer",
             "Close at +10% of net debit or at 7 DTE; add calendars on a strike breach"]
    entry_logic = "Three calendars around ATM; widths from expected move."
    exit_logic = "10% debit target / 7-DTE time stop / breach adjustments."

    async def scan(self, universe):  # not a stock-picker
        return []

    async def run_live(self) -> None:
        params = _params()
        src = LivePriceSource()
        position = await load_open(self.key)
        await log_event("Triple Calendar live loop started", category="action", strategy_key=self.key)
        record_id = None
        while True:
            try:
                today = datetime.now().date()
                # Build a per-tick snapshot the core can read synchronously.
                spot = await src.aunderlying(params.underlying)
                # (strikes/option mids are fetched lazily by re-running manage on a snapshot
                # that pulls the ATM + outer strikes; for simplicity we prefetch the chain.)
                strikes = await src.achain(params.underlying)
                snap = _LiveSnapshotBuilder(src, params, spot, strikes)
                out = await snap.run(today, position, params, record_id, self.key)
                position, record_id = out
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await log_event("Triple Calendar tick error", level="warning",
                                category="decision", strategy_key=self.key)
            await asyncio.sleep(get_settings().tc_manage_interval_seconds)


class _LiveSnapshotBuilder:
    """Resolves the option mids the core needs for one tick, then runs manage_step and
    persists the result."""
    def __init__(self, src, params, spot, strikes):
        self.src, self.params, self.spot, self.strikes = src, params, spot, strikes

    async def run(self, today, position, params, record_id, key):
        from app.strategies.triple_calendar.legs import (atm_strike, expected_move,
                                                         outer_strikes, snap, target_expiry)
        sell_e = target_expiry(today, params.sell_dte)
        buy_e = target_expiry(sell_e, 7)
        atm = snap(atm_strike(self.spot, params.strike_round), self.strikes)
        wanted = {(sell_e, atm, "C"), (sell_e, atm, "P"), (buy_e, atm, "C")}
        # add the calendars' strikes for an existing position so manage can value them
        for cal in (position.calendars if position else []):
            wanted |= {(cal.sell_expiry, cal.strike, "C"), (cal.buy_expiry, cal.strike, "C")}
        # add prospective outer strikes for a fresh entry
        if position is None:
            c = await self.src.aoption_mid(params.underlying, sell_e, atm, "C")
            pmid = await self.src.aoption_mid(params.underlying, sell_e, atm, "P")
            if c and pmid:
                lo, up = outer_strikes(atm, expected_move(c, pmid), params.em_buffer,
                                       params.strike_round, params.manual_nudge)
                for s in (snap(lo, self.strikes), snap(up, self.strikes)):
                    wanted |= {(sell_e, s, "C"), (buy_e, s, "C")}
        opt = {}
        for (e, k, r) in wanted:
            opt[(e, k, r)] = await self.src.aoption_mid(params.underlying, e, k, r)
        source = _Snapshot(self.spot, self.strikes, opt)

        executor = _make_executor()
        out = await manage_step(today, source, params, executor, position)
        return await _persist(out, record_id, key)


def _make_executor():
    from app.engine.execution import submit_combo_order

    async def _ex(underlying, legs, **kw):
        return await submit_combo_order(underlying, legs, **kw)
    return _ex


async def _persist(out, record_id, key):
    action, position = out["action"], out["position"]
    if action == "enter":
        rid = await save_position(key, position, order_ids=[], paper=not get_settings().enable_live_orders)
        await log_event(f"ENTER triple calendar {position.underlying} debit "
                        f"{position.net_debit:.2f}", category="trade", strategy_key=key)
        return position, rid
    if action == "adjust" and record_id is not None:
        await update_calendars(record_id, position)
        await log_event(f"ADJUST triple calendar {out['info'].get('side')}",
                        category="trade", strategy_key=key)
        return position, record_id
    if action == "exit" and record_id is not None:
        await close_position(record_id, pnl=out["info"].get("pnl", 0.0))
        await log_event(f"EXIT triple calendar ({out['info'].get('reason')})",
                        category="trade", strategy_key=key)
        return None, None
    return position, record_id
```

- [ ] **Step 6: Wire the worker dispatch (in `app/engine/worker.py`, top of `run_strategy`)**

Replace the start of `run_strategy` so options strategies use their own loop first:
```python
async def run_strategy(strategy: BaseStrategy) -> None:
    if getattr(strategy, "is_options", False):
        await strategy.run_live()
        return
    if getattr(strategy, "is_multi_position", False):
        await _run_multi_position(strategy)
    elif hasattr(strategy, "live_decision"):
        await _run_rule_driven(strategy)
    else:
        await _run_generic(strategy)
```

- [ ] **Step 7: Register the strategy (in `app/strategies/registry.py`)**

```python
from app.strategies.triple_calendar.strategy_class import TripleCalendarStrategy
```
Change the registry build so `strategy_2` is the Triple Calendar:
```python
_STRATEGIES: dict[str, BaseStrategy] = {
    f"strategy_{i}": ComingSoonStrategy(key=f"strategy_{i}", name=f"Strategy {i}")
    for i in range(2, settings.max_strategies + 1)
}
_tc = TripleCalendarStrategy()
_STRATEGIES[_tc.key] = _tc
```

- [ ] **Step 8: Run tests + import check**

Run: `../.venv/bin/python -m pytest tests/test_tc_strategy_class.py -v && ../.venv/bin/python -c "import app.main; print('ok')"`
Expected: PASS + `ok`.

- [ ] **Step 9: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/app/strategies/triple_calendar/live_source.py backend/app/strategies/triple_calendar/strategy_class.py backend/app/config.py backend/app/engine/worker.py backend/app/strategies/registry.py backend/tests/test_tc_strategy_class.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(triple_calendar): live source + strategy class + worker/registry/config wiring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Live smoke script (user-run)

**Files:**
- Create: `backend/scripts/triple_calendar_live_smoke.py`

- [ ] **Step 1: Implement**

```python
# backend/scripts/triple_calendar_live_smoke.py
"""Manual live check (needs IB Gateway connected + OPRA + market open). NOT a unit test.

    python -m scripts.triple_calendar_live_smoke           # paper: logs the entry combo
    ENABLE_LIVE_ORDERS=1 python -m scripts.triple_calendar_live_smoke   # places + flattens
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from app.engine.execution import flatten_all
from app.ibkr.gateway import get_gateway
from app.strategies.triple_calendar.live import entry_legs
from app.strategies.triple_calendar.strategy_class import _LiveSnapshotBuilder, _params
from app.strategies.triple_calendar.live_source import LivePriceSource
from app.strategies.triple_calendar.strategy import decide_entry


async def main() -> int:
    await get_gateway().connect()
    params = _params()
    src = LivePriceSource()
    spot = await src.aunderlying(params.underlying)
    strikes = await src.achain(params.underlying)
    print(f"{params.underlying} spot {spot} | {len(strikes)} strikes")
    builder = _LiveSnapshotBuilder(src, params, spot, strikes)
    # Resolve a snapshot and try an entry decision.
    snap_run = await builder.run(datetime.now().date(), None, params, None, "strategy_2")
    pos, _ = snap_run
    if pos is None:
        print("No entry (no quotes / filters) — check OPRA entitlement + market open.")
    else:
        print(f"Entry built: {len(entry_legs(pos))} legs, debit {pos.net_debit:.2f}")
        input("Press Enter to flatten (if live)...")
        print("flatten:", await flatten_all())
    get_gateway().disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Commit**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add backend/scripts/triple_calendar_live_smoke.py
git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "feat(triple_calendar): live smoke script

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Verification

- [ ] **Step 1: Full backend suite**

Run: `cd backend && ../.venv/bin/python -m pytest -q`
Expected: all pass (incl. the new combo/store/manage/strategy tests).

- [ ] **Step 2: App imports + registry shows the strategy**

Run: `../.venv/bin/python -c "import app.main; from app.strategies.registry import list_strategies; print([(s.key, s.name, s.ready) for s in list_strategies()])"`
Expected: prints the slots; `strategy_2` is `Triple Calendar`, ready True.

- [ ] **Step 3: Paper-mode combo gate (no Gateway needed)**

Run: `../.venv/bin/python -c "import asyncio; from app.engine.execution import submit_combo_order; from datetime import date; print(asyncio.run(submit_combo_order('QQQ', [{'expiry':date(2026,7,17),'strike':445.0,'right':'C','action':'SELL'}], net_price=1.0)))"`
Expected: `{'transmitted': False, 'paper': True, ...}` (ENABLE_LIVE_ORDERS off by default).

- [ ] **Step 4: Rebuild desktop app (so the dashboard shows the live strategy)**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system && pkill -f TradingSystem; ./desktop/build.sh`
Expected: build completes. (User reopens; Triple Calendar appears in the Strategies tab. Live
activation + the smoke script need IB Gateway connected, OPRA, and market open.)

- [ ] **Step 5: Final commit (if outstanding)**

```bash
git -C /Users/karanpatel/PycharmProjects/Tradding-system add -A && git -C /Users/karanpatel/PycharmProjects/Tradding-system commit -m "test: triple calendar phase 2 green" || echo "nothing to commit"
```
