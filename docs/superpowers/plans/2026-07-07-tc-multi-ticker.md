# Multi-ticker Triple Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Triple Calendar on a UI-editable list of tickers, each with its own independent position and per-ticker contracts, from a single live loop.

**Architecture:** Positions are keyed by `(strategy_key, underlying)` (the column already exists). The store gains an `underlying` filter + a `list_open`. The live loop iterates a configured ticker list each tick — fetch VIX once, then manage/enter/roll each ticker's own position at its own contracts. A `tc_tickers` JSON setting (editable via a new endpoint + UI) drives it; empty → today's single-ticker behavior. The options-position endpoint returns a per-ticker list; the UI shows a management card + one position card per ticker.

**Tech Stack:** Python 3.14, FastAPI, async SQLAlchemy (SQLite), ib_async; React + Vite.

## Global Constraints

- **Run Python from `backend/`** with `../.venv/bin/python`. Pytest style; async tests `@pytest.mark.asyncio`; DB tests set `DATABASE_URL=sqlite+aiosqlite:///./_t_<name>.db` and `await init_db()`. Standalone loop tests are run scripts (`python -m tests.<name>`), like `test_tc_vix_live.py`.
- **Branch:** `tc-multi-ticker` — commit there; do NOT create/switch branches.
- **Back-compat:** when `StrategyState.tc_tickers` is empty, behavior is unchanged — one implicit ticker `{ticker: settings.tc_underlying, contracts: StrategyState.contracts or 1}`.
- **Per-ticker contracts** (from `tc_tickers`) is authoritative for TC sizing; the global `StrategyState.contracts` applies only to the empty-list fallback.
- **Positions independent per ticker:** each ticker enters/rolls/exits/adjusts on its own; VIX (≤16 gate) and event dates are market-wide/shared.
- Ticker list: uppercase, contracts clamped ≥1, de-duplicated, capped at 10.
- Style: `from __future__ import annotations`, module docstrings.

---

### Task 1: Store — key open lookups by underlying + `list_open`

**Files:**
- Modify: `backend/app/strategies/triple_calendar/store.py` (`load_open`, `load_open_id`, add `list_open`)
- Test: `backend/tests/test_tc_multi_store.py`

**Interfaces:**
- Produces:
  - `load_open(strategy_key, underlying: str | None = None) -> Position | None` — when `underlying` is given, only that underlying's open record; when `None`, the first open record (back-compat).
  - `load_open_id(strategy_key, underlying: str | None = None) -> int | None` — same filtering.
  - `list_open(strategy_key) -> list[OptionPositionRecord]` — all open records for the strategy.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_multi_store.py
from datetime import date

import pytest

from app.db.base import init_db
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import (list_open, load_open, load_open_id,
                                                  save_position)

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


@pytest.mark.asyncio
async def test_open_lookups_key_by_underlying():
    await init_db()
    q = Position("QQQ", date(2026, 6, 25), [Calendar(445.0, SE, BE)], net_debit=1.5)
    s = Position("SPY", date(2026, 6, 25), [Calendar(560.0, SE, BE)], net_debit=2.5)
    qid = await save_position("strategy_2", q, order_ids=[], paper=True)
    sid = await save_position("strategy_2", s, order_ids=[], paper=True)

    # underlying-filtered lookups return the right one
    assert (await load_open("strategy_2", "QQQ")).underlying == "QQQ"
    assert (await load_open("strategy_2", "SPY")).underlying == "SPY"
    assert await load_open("strategy_2", "IWM") is None
    assert await load_open_id("strategy_2", "QQQ") == qid
    assert await load_open_id("strategy_2", "SPY") == sid

    # list_open returns all; no-arg load_open still returns *a* record (back-compat)
    opens = await list_open("strategy_2")
    assert {r.underlying for r in opens} == {"QQQ", "SPY"}
    assert await load_open("strategy_2") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_ms.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ms.db ../.venv/bin/python -m pytest tests/test_tc_multi_store.py -q; rm -f _t_ms.db`
Expected: FAIL — `load_open()` takes 1 arg / `list_open` undefined.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/store.py`, replace `load_open` and `load_open_id` and add `list_open`:

```python
async def load_open(strategy_key: str, underlying: str | None = None) -> Position | None:
    async with session_scope() as session:
        stmt = select(OptionPositionRecord).where(
            OptionPositionRecord.strategy_key == strategy_key,
            OptionPositionRecord.status == "open")
        if underlying is not None:
            stmt = stmt.where(OptionPositionRecord.underlying == underlying.upper())
        rec = (await session.execute(stmt)).scalars().first()
        if rec is None:
            return None
        pos = _deser(rec.underlying, rec.entry_time, rec.net_debit, rec.calendars)
        pos.contracts = rec.contracts or 1
        return pos


async def load_open_id(strategy_key: str, underlying: str | None = None) -> int | None:
    async with session_scope() as session:
        stmt = select(OptionPositionRecord.id).where(
            OptionPositionRecord.strategy_key == strategy_key,
            OptionPositionRecord.status == "open")
        if underlying is not None:
            stmt = stmt.where(OptionPositionRecord.underlying == underlying.upper())
        return (await session.execute(stmt)).scalars().first()


async def list_open(strategy_key: str) -> list:
    async with session_scope() as session:
        return list((await session.execute(
            select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == strategy_key,
                OptionPositionRecord.status == "open"))).scalars().all())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_ms.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ms.db ../.venv/bin/python -m pytest tests/test_tc_multi_store.py -q; rm -f _t_ms.db`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/store.py backend/tests/test_tc_multi_store.py
git commit -m "feat(tc-multi): store open lookups filter by underlying + list_open"
```

---

### Task 2: `StrategyState.tc_tickers` column + migration

**Files:**
- Modify: `backend/app/db/models.py` (`StrategyState`)
- Modify: `backend/app/db/base.py` (`_ensure_added_columns`)
- Test: `backend/tests/test_tc_tickers_column.py`

**Interfaces:**
- Produces: `StrategyState.tc_tickers: list` (JSON, default `[]`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_tickers_column.py
import pytest

from app.db.base import init_db, session_scope
from app.db.models import StrategyState


@pytest.mark.asyncio
async def test_tc_tickers_round_trip():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
            s.add(st)
        assert st.tc_tickers == []  # default
        st.tc_tickers = [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 1}]
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        assert st.tc_tickers[0]["ticker"] == "QQQ" and st.tc_tickers[1]["contracts"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_tc.db && DATABASE_URL=sqlite+aiosqlite:///./_t_tc.db ../.venv/bin/python -m pytest tests/test_tc_tickers_column.py -q; rm -f _t_tc.db`
Expected: FAIL — no `tc_tickers` attribute.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/db/models.py`, in `StrategyState`, add after the `contracts` column:
```python
    tc_tickers: Mapped[list] = mapped_column(JSON, default=list)  # [{ticker, contracts}] for multi-ticker TC
```
(`JSON` is already imported in this file.)

In `backend/app/db/base.py`, in `_ensure_added_columns`, in the `strategy_state` block (after the `contracts` guard) add:
```python
        if "tc_tickers" not in cols:
            conn.execute(text("ALTER TABLE strategy_state ADD COLUMN tc_tickers JSON DEFAULT '[]'"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_tc.db && DATABASE_URL=sqlite+aiosqlite:///./_t_tc.db ../.venv/bin/python -m pytest tests/test_tc_tickers_column.py -q; rm -f _t_tc.db`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/db/base.py backend/tests/test_tc_tickers_column.py
git commit -m "feat(tc-multi): StrategyState.tc_tickers column + migration"
```

---

### Task 3: `GET/PUT /tickers` endpoint + `detail.tc_tickers`

**Files:**
- Modify: `backend/app/routers/strategies.py` (add `_normalize_tickers`, `TickersBody`, `get_tickers`, `set_tickers`; add `tc_tickers` to `detail`)
- Test: `backend/tests/test_tc_tickers_api.py`

**Interfaces:**
- Consumes: `StrategyState.tc_tickers` (Task 2).
- Produces:
  - `_normalize_tickers(raw: list) -> list[dict]` — uppercase ticker, contracts `max(1,int)`, drop blanks, de-dupe (first wins), cap 10.
  - `PUT /api/strategies/{key}/tickers` body `{tickers: [{ticker, contracts}]}` (handler `set_tickers`, model `TickersBody`, item model `TickerItem`) → `{key, tickers}`.
  - `GET /api/strategies/{key}/tickers` (handler `get_tickers`) → `{key, tickers}`.
  - `detail` response includes `"tc_tickers": st.tc_tickers if st else []`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_tickers_api.py
import pytest

from app.db.base import init_db
from app.routers import strategies


@pytest.mark.asyncio
async def test_set_tickers_normalizes_and_persists():
    await init_db()
    body = strategies.TickersBody(tickers=[
        strategies.TickerItem(ticker="qqq", contracts=2),
        strategies.TickerItem(ticker="SPY", contracts=0),   # clamps to 1
        strategies.TickerItem(ticker="qqq", contracts=9),   # dup -> dropped (first wins)
        strategies.TickerItem(ticker="", contracts=1),      # blank -> dropped
    ])
    out = await strategies.set_tickers("strategy_2", body)
    assert out["tickers"] == [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 1}]
    assert (await strategies.get_tickers("strategy_2"))["tickers"] == out["tickers"]
    assert (await strategies.detail("strategy_2"))["tc_tickers"] == out["tickers"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_ta.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ta.db ../.venv/bin/python -m pytest tests/test_tc_tickers_api.py -q; rm -f _t_ta.db`
Expected: FAIL — `TickersBody`/`set_tickers` undefined.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/routers/strategies.py`, add near the `ContractsBody` class:

```python
class TickerItem(BaseModel):
    ticker: str
    contracts: int = 1


class TickersBody(BaseModel):
    tickers: list[TickerItem]


def _normalize_tickers(raw: list) -> list[dict]:
    """Uppercase, clamp contracts >=1, drop blanks, de-dupe (first wins), cap at 10."""
    out: list[dict] = []
    seen: set[str] = set()
    for it in raw:
        t = (it.ticker or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append({"ticker": t, "contracts": max(1, int(it.contracts))})
        if len(out) >= 10:
            break
    return out


@router.get("/{key}/tickers")
async def get_tickers(key: str) -> dict:
    st = await _state(key)
    return {"key": key, "tickers": (st.tc_tickers if st else []) or []}


@router.put("/{key}/tickers")
async def set_tickers(key: str, body: TickersBody) -> dict:
    strat = _require_ready(key)
    tickers = _normalize_tickers(body.tickers)
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st is None:
            st = StrategyState(key=key, name=strat.name, ready=True)
            session.add(st)
        st.tc_tickers = tickers
    await log_event(f"TC tickers set: {', '.join(t['ticker'] for t in tickers) or '(none)'}",
                    category="action", strategy_key=key)
    return {"key": key, "tickers": tickers}
```

In `detail`'s returned dict, add (next to `"contracts"`):
```python
        "tc_tickers": (st.tc_tickers if st else []) or [],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_ta.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ta.db ../.venv/bin/python -m pytest tests/test_tc_tickers_api.py -q; rm -f _t_ta.db`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/strategies.py backend/tests/test_tc_tickers_api.py
git commit -m "feat(tc-multi): GET/PUT /tickers endpoint + detail.tc_tickers"
```

---

### Task 4: Live loop — iterate tickers, one position each

**Files:**
- Modify: `backend/app/strategies/triple_calendar/strategy_class.py` (`_params`, add `_event_dates`/`_tickers`, rewrite `run_live`)
- Test: `backend/tests/test_tc_multi_live.py`

**Interfaces:**
- Consumes: `store.load_open(key, underlying)`, `store.load_open_id(key, underlying)`, `store.list_open(key)` (Task 1); `StrategyState.tc_tickers` (Task 2); `manage_step(..., contracts=)`.
- Produces: `run_live` manages a `dict[ticker -> Position|None]` + `dict[ticker -> record_id]`; `_tickers()` returns `list[{ticker, contracts}]` (fallback to `tc_underlying`/global contracts when empty); `_params(underlying, events)` builds per-ticker params.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_multi_live.py
import asyncio


def _run():
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import OptionPositionRecord, StrategyState
        from app.ibkr.gateway import ConnState, get_gateway
        from app.strategies.triple_calendar import live_source, strategy_class
        from sqlalchemy import select

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02
        get_settings().vix_wait_poll_seconds = 0.02
        async with session_scope() as s:
            st = await s.get(StrategyState, "strategy_2")
            if st is None:
                st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
                s.add(st)
            st.tc_tickers = [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 3}]

        async def a_spot(self, sym): return 445.0
        async def a_chain(self, u): return [float(r) for r in range(400, 491, 5)]
        async def a_mid(self, u, e, k, r): return 1.0 + 0.1 * max((e - __import__("datetime").date.today()).days, 0)
        async def a_vix(self): return 12.0
        live_source.LivePriceSource.aunderlying = a_spot
        live_source.LivePriceSource.achain = a_chain
        live_source.LivePriceSource.aoption_mid = a_mid
        live_source.LivePriceSource.avix = a_vix
        strategy_class.get_market_status = lambda: {"is_open": True}
        get_gateway().set_state(ConnState.READY)

        strat = strategy_class.TripleCalendarStrategy()
        task = asyncio.create_task(strat.run_live())
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        async with session_scope() as s:
            recs = (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == "strategy_2",
                OptionPositionRecord.status == "open"))).scalars().all()
        by = {r.underlying: r for r in recs}
        assert set(by) == {"QQQ", "SPY"}, f"expected both tickers open, got {set(by)}"
        assert by["QQQ"].contracts == 2 and by["SPY"].contracts == 3
        print(f"PASS: both tickers opened independently (QQQ x{by['QQQ'].contracts}, SPY x{by['SPY'].contracts})")

    asyncio.run(_t())
    print("\nALL TC-MULTI-LIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_ml.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ml.db ../.venv/bin/python -m tests.test_tc_multi_live 2>&1 | tail -4; rm -f _t_ml.db`
Expected: FAIL — only one (or zero) open records; the loop is still single-ticker.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/strategy_class.py`:

Replace `_params()` with an underlying-parameterized version + an events helper:
```python
def _event_dates() -> list[date]:
    s = get_settings()
    return [date.fromisoformat(d.strip()) for d in s.tc_event_dates.split(",") if d.strip()]


def _params(underlying: str, events: list[date]) -> TripleCalendarParams:
    return TripleCalendarParams(underlying=underlying, event_dates=events)
```

Add a `_tickers` method on the strategy class (next to `_contracts`):
```python
    async def _tickers(self) -> list[dict]:
        from app.db.base import session_scope
        from app.db.models import StrategyState
        async with session_scope() as s:
            st = await s.get(StrategyState, self.key)
        raw = (st.tc_tickers if st else None) or []
        cleaned = [{"ticker": str(t["ticker"]).upper(), "contracts": max(1, int(t.get("contracts", 1)))}
                   for t in raw if t.get("ticker")]
        if cleaned:
            return cleaned
        # Fallback: single configured underlying at the global contracts setting.
        contracts = max(1, int(getattr(st, "contracts", 1) or 1)) if st else 1
        return [{"ticker": get_settings().tc_underlying, "contracts": contracts}]
```

Replace the whole `run_live` method body with the multi-ticker loop:
```python
    async def run_live(self) -> None:
        src = LivePriceSource()
        events = _event_dates()
        executor = _make_executor()
        # Seed per-ticker state from any open records (restart-while-holding).
        positions: dict = {}
        record_ids: dict = {}
        for rec in await list_open(self.key):
            positions[rec.underlying] = await load_open(self.key, rec.underlying)
            record_ids[rec.underlying] = rec.id
        await log_event("Triple Calendar live loop started", category="action",
                        strategy_key=self.key)
        while True:
            if get_gateway().state is not ConnState.READY or not get_market_status()["is_open"]:
                await asyncio.sleep(get_settings().tc_manage_interval_seconds)
                continue
            vix = None
            tickers = await self._tickers()
            try:
                today = datetime.now().date()
                vix = await src.avix()
                for tk in tickers:
                    u, contracts = tk["ticker"], tk["contracts"]
                    params = _params(u, events)
                    pos = positions.get(u)
                    rid = record_ids.get(u)
                    try:
                        spot = await src.aunderlying(u)
                        if spot is None:
                            continue
                        if pos is None and vix is None:
                            await log_event(f"Triple Calendar {u}: skipping entry — VIX "
                                            "unavailable (fail-closed)", category="decision",
                                            strategy_key=self.key)
                            continue
                        strikes = await src.achain(u)
                        snapshot = await _resolve_snapshot(src, params, spot, strikes, today, pos)
                        out = await manage_step(today, snapshot, params, executor, pos,
                                                vix=vix, contracts=contracts)
                        pos, rid = await self._persist(out, rid)
                        positions[u], record_ids[u] = pos, rid
                        if pos is not None and rid is not None and pos.net_debit:
                            from app.strategies.triple_calendar.strategy import position_value
                            val = position_value(snapshot, pos, today)
                            if val is not None:
                                await update_value(rid, val,
                                                   (val - pos.net_debit) / pos.net_debit, spot)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — one ticker's error must not stop the rest
                        await log_event(f"Triple Calendar {u} tick error", level="warning",
                                        category="decision", strategy_key=self.key)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await log_event("Triple Calendar tick error", level="warning",
                                category="decision", strategy_key=self.key)
            # Fast-poll while any ticker is flat AND VIX blocks entry; else the manage interval.
            vmax = TripleCalendarParams().vix_entry_max
            any_flat_blocked = (vix is None or vix > vmax) and any(
                positions.get(tk["ticker"]) is None for tk in tickers)
            poll = (get_settings().vix_wait_poll_seconds if any_flat_blocked
                    else get_settings().tc_manage_interval_seconds)
            await asyncio.sleep(poll)
```

Note: keep the existing imports (`load_open`, `load_open_id`, `list_open`, `manage_step`, `_resolve_snapshot`, `update_value`, `get_settings`, `TripleCalendarParams`, etc.) — add `list_open` to the `store` import line, and remove the now-unused `load_open_id` import only if nothing references it (the seed loop uses `rec.id` directly, so `load_open_id` may become unused — leave the import if other code still needs it; otherwise drop it to avoid an unused-import).

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd backend && rm -f _t_ml.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ml.db ../.venv/bin/python -m tests.test_tc_multi_live 2>&1 | tail -3; rm -f _t_ml.db
../.venv/bin/python -c "import app.main; print('main imports OK')"
```
Expected: `ALL TC-MULTI-LIVE TESTS PASSED`; `main imports OK`. Then confirm the single-ticker standalone tests still pass:
```bash
for t in test_tc_vix_live test_tc_contracts_live test_tc_roll_live test_tc_live_persist; do
  rm -f _t_$t.db; echo -n "$t: "; DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -cE "ALL .* PASSED" | sed 's/^1$/PASS/;s/^0$/FAIL/'; rm -f _t_$t.db
done
```
Expected: all PASS (empty `tc_tickers` → the fallback single ticker preserves their behavior).

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/strategy_class.py backend/tests/test_tc_multi_live.py
git commit -m "feat(tc-multi): run_live iterates the ticker list, one independent position each"
```

---

### Task 5: options-position endpoint → per-ticker list

**Files:**
- Modify: `backend/app/routers/strategies.py` (extract `_serialize_option_position`; rewrite `options_position` to return `{tickers: [...], ...}`)
- Test: `backend/tests/test_tc_multi_endpoint.py`

**Interfaces:**
- Consumes: `store.list_open` isn't needed here (query directly); `_normalize_tickers`/`_tickers`-equivalent list from `StrategyState.tc_tickers` with fallback.
- Produces: `options_position(key)` returns `{tickers: [ {ticker, contracts, has_position, <position fields when open>} ], running, broker_connected, conn_ready, market_open, live_orders_enabled, last_signal_time, last_trade_time}`. Extract the current per-position body into `_serialize_option_position(rec, contracts) -> dict` returning ONLY the position fields (has_position True + net_debit, calendars, target/stop, value, unrealized_pnl ×contracts, breach, progress, stale, etc.) — i.e. everything the current `out` dict has EXCEPT the `base`/status fields.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_multi_endpoint.py
from datetime import date, timedelta

import pytest

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.routers import strategies


@pytest.mark.asyncio
async def test_options_position_lists_all_tickers():
    await init_db()
    today = date.today()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
            s.add(st)
        st.tc_tickers = [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 1}]
        # QQQ has an open position; SPY is flat
        s.add(OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="open", paper=True,
            net_debit=4.0, contracts=2, last_value=4.2, last_pnl_pct=0.05, last_spot=501.2,
            calendars=[{"strike": 500.0, "sell": (today + timedelta(days=21)).isoformat(),
                        "buy": (today + timedelta(days=28)).isoformat()}], order_ids=[]))
    out = await strategies.options_position("strategy_2")
    by = {t["ticker"]: t for t in out["tickers"]}
    assert set(by) == {"QQQ", "SPY"}
    assert by["QQQ"]["has_position"] is True
    assert by["QQQ"]["unrealized_pnl"] == pytest.approx(40.0)   # (4.2-4.0)*100*2
    assert by["SPY"]["has_position"] is False and by["SPY"]["contracts"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_me.db && DATABASE_URL=sqlite+aiosqlite:///./_t_me.db ../.venv/bin/python -m pytest tests/test_tc_multi_endpoint.py -q; rm -f _t_me.db`
Expected: FAIL — `out["tickers"]` doesn't exist (endpoint returns the old single-position shape).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/routers/strategies.py`, refactor `options_position`:

1. **Extract** the current position-serialization logic into a helper. Take the existing body of `options_position` — everything from `p = TripleCalendarParams()` down to (but not including) the final `return out` — and move it into:
```python
def _serialize_option_position(rec, contracts: int) -> dict:
    """Position fields for one open OptionPositionRecord (P&L scaled by contracts). No base/status."""
    from app.strategies.triple_calendar.params import TripleCalendarParams
    p = TripleCalendarParams()
    today = date.today()
    net = rec.net_debit or 0.0
    # ... (the existing calendars/value/stale/out-building/breach logic, VERBATIM) ...
    # In the returned dict use the passed `contracts` (not rec.contracts) and DROP the
    # `**base` spread; include "has_position": True, "ticker": rec.underlying, "contracts": contracts.
    return out
```
Concretely: keep the existing computations (`calendars`, `value/pnl_pct/spot/valued_at/stale`, `out` dict with `net_debit`, `target_value`, `stop_value`, `profit_target_pct`, `stop_loss_pct`, `vix_threshold`, `value`, `unrealized_pct`, `unrealized_pnl = round((value-net)*100*contracts,2)`, `spot`, `valued_at`, `stale`, `progress_to_target`, `distance_to_stop`, `breach`, `underlying`, `status`, `paper`, `entry_time`, `days_held`). Replace `**base,` with `"has_position": True, "ticker": rec.underlying, "contracts": contracts,` and drop the `rec.contracts or 1` local in favor of the passed `contracts`.

2. **Rewrite** `options_position` to iterate the configured tickers:
```python
@router.get("/{key}/options-position")
async def options_position(key: str) -> dict:
    _require_ready(key)
    running = engine_manager.is_running(key)
    conn_ready = _conn_ready()
    _, market_open = _market_open()
    last_signal, last_trade = await _last_event_times(key)
    base = {
        "running": running,
        "broker_connected": session_manager.is_authenticated(),
        "conn_ready": conn_ready,
        "market_open": market_open,
        "live_orders_enabled": get_settings().enable_live_orders,
        "last_signal_time": last_signal,
        "last_trade_time": last_trade,
    }
    st = await _state(key)
    raw = (st.tc_tickers if st else None) or []
    tickers = [{"ticker": str(t["ticker"]).upper(), "contracts": max(1, int(t.get("contracts", 1)))}
               for t in raw if t.get("ticker")]
    if not tickers:
        contracts = max(1, int(getattr(st, "contracts", 1) or 1)) if st else 1
        tickers = [{"ticker": (get_settings().tc_underlying or "QQQ").upper(), "contracts": contracts}]

    from sqlalchemy import select

    from app.db.models import OptionPositionRecord
    out_tickers = []
    async with session_scope() as session:
        for tk in tickers:
            rec = (await session.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == key,
                OptionPositionRecord.underlying == tk["ticker"],
                OptionPositionRecord.status == "open"))).scalars().first()
            if rec is None:
                out_tickers.append({"ticker": tk["ticker"], "contracts": tk["contracts"],
                                    "has_position": False})
            else:
                out_tickers.append(_serialize_option_position(rec, tk["contracts"]))
    return {**base, "tickers": out_tickers}
```
Remove the now-unused `_open_option_record` helper if nothing else references it (grep first; the equity paths use `_open_position`, not this).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_me.db && DATABASE_URL=sqlite+aiosqlite:///./_t_me.db ../.venv/bin/python -m pytest tests/test_tc_multi_endpoint.py tests/test_tc_options_endpoint.py -q; rm -f _t_me.db`
Expected: the new test passes. NOTE: `test_tc_options_endpoint.py` asserts the OLD single-position shape (`has_position` at top level) — update it in this task to read the new shape: a single configured/fallback ticker now appears as `out["tickers"][0]`. Change its assertions from `out["has_position"]` to `out["tickers"][0]["has_position"]` and the flat/position field reads accordingly (the fields are the same, just nested under `tickers[0]`). Re-run until both pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/strategies.py backend/tests/test_tc_multi_endpoint.py backend/tests/test_tc_options_endpoint.py
git commit -m "feat(tc-multi): options-position returns a per-ticker list"
```

---

### Task 6: Frontend — Tickers management card + api

**Files:**
- Modify: `frontend/src/api.js` (add `setTickers`)
- Modify: `frontend/src/components/OptionsStrategyDetail.jsx` (Tickers card)

**Interfaces:**
- Consumes: `PUT /api/strategies/{key}/tickers` (Task 3); `detail.tc_tickers` (Task 3).

- [ ] **Step 1: Add the API method**

In `frontend/src/api.js`, after `setContracts`, add:
```javascript
  // Multi-ticker Triple Calendar: set the full ticker list [{ticker, contracts}].
  setTickers: (key, tickers) =>
    request(`/api/strategies/${key}/tickers`, {
      method: "PUT",
      body: JSON.stringify({ tickers }),
    }),
```

- [ ] **Step 2: Add the Tickers card**

In `frontend/src/components/OptionsStrategyDetail.jsx`, add state near the other hooks (after `const [contracts, setContracts] = useState("");`):
```javascript
  const [tickerRows, setTickerRows] = useState(null); // null until seeded from detail
  useEffect(() => {
    if (detail && tickerRows === null) {
      const rows = (detail.tc_tickers && detail.tc_tickers.length)
        ? detail.tc_tickers
        : [{ ticker: (op?.tickers?.[0]?.ticker) || "QQQ", contracts: detail.contracts ?? 1 }];
      setTickerRows(rows.map((r) => ({ ...r })));
    }
  }, [detail, op, tickerRows]);
```
Then render a Tickers card (place it right after the Controls `</section>`):
```javascript
      {tickerRows && (
        <section className="card">
          <h2>Tickers</h2>
          <div className="muted small">Each ticker runs its own Triple Calendar position at its own contract size.</div>
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Ticker</th><th>Contracts</th><th></th></tr></thead>
              <tbody>
                {tickerRows.map((r, i) => (
                  <tr key={i}>
                    <td><input className="input tiny" value={r.ticker}
                      onChange={(e) => setTickerRows((rs) => rs.map((x, j) => j === i ? { ...x, ticker: e.target.value.toUpperCase() } : x))} /></td>
                    <td><input className="input tiny" type="number" min="1" value={r.contracts}
                      onChange={(e) => setTickerRows((rs) => rs.map((x, j) => j === i ? { ...x, contracts: Number(e.target.value) } : x))} /></td>
                    <td><button className="btn ghost tiny" onClick={() => setTickerRows((rs) => rs.filter((_, j) => j !== i))}>✕</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row gap center">
            <button className="btn ghost" onClick={() => setTickerRows((rs) => [...rs, { ticker: "", contracts: 1 }])}>+ Add ticker</button>
            <button className="btn primary" onClick={() => act(() => api.setTickers(keyName,
              tickerRows.filter((r) => r.ticker.trim()).map((r) => ({ ticker: r.ticker.trim().toUpperCase(), contracts: Math.max(1, Number(r.contracts) || 1) }))))}>Save tickers</button>
          </div>
        </section>
      )}
```

- [ ] **Step 3: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.js frontend/src/components/OptionsStrategyDetail.jsx
git commit -m "feat(tc-multi): Tickers management card + setTickers api"
```

---

### Task 7: Frontend — one position card per ticker

**Files:**
- Modify: `frontend/src/components/OptionsStrategyDetail.jsx` (render `op.tickers` as position cards)

**Interfaces:**
- Consumes: `op.tickers` (Task 5 endpoint shape).

- [ ] **Step 1: Render a card per ticker**

In `frontend/src/components/OptionsStrategyDetail.jsx`, the "Open Position" section currently reads `hasPos = op?.has_position` and renders one card. Replace that whole Open-Position block with a map over `op?.tickers`:
- Compute `const tks = op?.tickers || [];`.
- For each `t` in `tks`, render (in a `.map`, `key={t.ticker}`):
  - a heading `Open Position — {t.ticker} · {t.contracts} contract(s)`;
  - if `t.has_position`: the **existing** position-card body (the P&L row, target/stop meter, Calendars table, breach) — reuse the current markup verbatim, but read every field off `t` instead of `op` (e.g. `t.unrealized_pnl`, `t.calendars`, `t.value`, `t.progress_to_target`, `t.breach`, etc.);
  - else: a compact "scanning" row: `No open position — scanning {t.ticker} for entry (VIX ≤ 16).`
- Remove the old top-level `hasPos`/`op.has_position` reads and the single flat/position blocks they gated.
- Guard: while `op` is null (first load), render nothing for this section.

(The helper functions `money`, `money0`, `pct`, `clamp01` at the top of the file stay and are reused per ticker.)

- [ ] **Step 2: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/OptionsStrategyDetail.jsx
git commit -m "feat(tc-multi): render one position card per ticker"
```

---

### Task 8: Full check + live verification

**Files:** none (verification only).

- [ ] **Step 1: Backend suite + import + standalone**

Run:
```bash
cd backend
../.venv/bin/python -c "import app.main; print('main imports OK')"
rm -f _t_all.db
DATABASE_URL=sqlite+aiosqlite:///./_t_all.db ../.venv/bin/python -m pytest \
  tests/test_tc_multi_store.py tests/test_tc_tickers_column.py tests/test_tc_tickers_api.py \
  tests/test_tc_multi_endpoint.py tests/test_tc_options_endpoint.py tests/test_contracts_api.py \
  tests/test_tc_manage_step.py tests/test_tc_roll.py -q
rm -f _t_all.db
for t in test_tc_multi_live test_tc_vix_live test_tc_contracts_live test_tc_roll_live test_tc_live_persist; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -cE "ALL .* PASSED" | sed 's/^1$/PASS/;s/^0$/FAIL/'; rm -f _t_$t.db
done
```
Expected: `main imports OK`; all pytest pass; each standalone prints PASS.

- [ ] **Step 2: Rebuild frontend + restart + smoke the endpoints**

Run:
```bash
cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep "built in"
cd .. && launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
for i in $(seq 1 45); do curl -s -m3 http://127.0.0.1:8765/health 2>/dev/null | grep -q '"status":"ok"' && break; sleep 1; done
curl -s -m5 -X PUT http://127.0.0.1:8765/api/strategies/strategy_2/tickers -H "Content-Type: application/json" -d '{"tickers":[{"ticker":"QQQ","contracts":1},{"ticker":"SPY","contracts":1}]}'
echo ""
curl -s -m10 http://127.0.0.1:8765/api/strategies/strategy_2/options-position | python3 -c "import sys,json;d=json.load(sys.stdin);print('tickers:',[(t['ticker'],t['has_position']) for t in d['tickers']])"
```
Expected: PUT returns the normalized 2-ticker list; options-position returns two ticker entries (both `has_position: false` while flat/market-closed).

- [ ] **Step 3: Reset the demo config + manual check**

Reset the ticker list to empty (back to single-ticker default) so the running strategy isn't left with a demo config:
```bash
curl -s -m5 -X PUT http://127.0.0.1:8765/api/strategies/strategy_2/tickers -H "Content-Type: application/json" -d '{"tickers":[]}' && echo " (reset)"
```
Then in the app: Strategies → Triple Calendar → the **Tickers** card (add/remove, per-ticker contracts) and one **Open Position** card per ticker. (Positions fill during RTH with the Gateway connected + VIX ≤ 16.)

- [ ] **Step 4: Final commit (only if manual tweaks were needed)**

```bash
git add -A && git commit -m "test(tc-multi): verify end-to-end" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** store underlying-keyed + list_open (T1); tc_tickers column+migration (T2); GET/PUT tickers + detail (T3); loop iterates tickers, per-ticker position/contracts, fail-closed + adaptive cadence (T4); endpoint per-ticker list w/ P&L×contracts + fallback (T5); Tickers UI (T6); per-ticker position cards (T7); regression+live (T8). All covered.
- **Back-compat:** empty `tc_tickers` → single fallback ticker in loop (T4) and endpoint (T5); existing single-ticker standalone tests re-run in T4/T8.
- **Type consistency:** ticker item shape `{ticker, contracts}` everywhere; `load_open(key, underlying=None)`; endpoint `{tickers:[{ticker, contracts, has_position, ...}]}` consumed by T6/T7; `_serialize_option_position(rec, contracts)`.
- **Placeholder scan:** T5's `_serialize_option_position` says "extract the existing body verbatim" — that references real current code in the same file, not a placeholder; the wrapper and field list are given explicitly.
