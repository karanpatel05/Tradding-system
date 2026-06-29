# IBKR Resilience — Phase 2a (Data Continuity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After any reconnect the system reliably reaches READY, active symbols' caches refresh promptly, and no worker opens a position on a non-contiguous price series (per-symbol gating).

**Architecture:** A pure gap gate (`is_stale`) plus an interval map; a best-effort `backfill_active()` that force-refreshes active strategies' bars via the existing provider + cache; a supervisor seam fix driving `CONNECTED → READY`; and worker integration that skips ENTRIES (never exits) on a stale series.

**Tech Stack:** Python 3.14, asyncio, `ib_async` 2.1.0, async SQLAlchemy, existing `IBKRHistoricalProvider` + `history_cache` + `market` services.

**Source spec:** `docs/superpowers/specs/2026-06-29-ibkr-resilience-phase2a-design.md`

## Global Constraints

- Reuse `app.providers.get_historical_provider()` (`IBKRHistoricalProvider`, `name="ibkr"`,
  `async get_bars(symbol, timeframe, start=None, end=None) -> list[dict]`) and
  `app.services.history_cache.save(symbol, interval, bars, source="")`. Do NOT build a parallel
  fetch/merge stack.
- Market-open comes from `app.services.market.get_market_status()["is_open"]` (bool).
- `backfill_active` is best-effort: per-spec exceptions are logged and skipped, never raised.
  Backfill is launched non-blocking from recovery so it never delays READY.
- The gap gate skips **entries only** (`arm`/`enter`); **exits are never gated** (must always be
  able to reduce risk).
- New strategy hooks are additive with safe defaults so existing strategies/tests are unaffected.
- Tests are offline against stubs. Test style: a module-level `async def _run()` (or sync for pure
  helpers) with `assert` + `print("PASS: …")`, ending `print("\nALL … TESTS PASSED")`, run via
  `DATABASE_URL=sqlite+aiosqlite:///./_t.db python -m tests.<name>`.

## File structure
- Create `app/services/backfill.py` — gap gate (`is_stale`), `interval_seconds`, `backfill_active`.
- Modify `app/strategies/base.py` — additive `backfill_specs`, `bar_interval` (defaults).
- Modify `app/strategies/ema_scalper/strategy.py` + `ema_crossover/strategy.py` — override hooks.
- Modify `app/ibkr/gateway.py` — extract `_supervise_tick`, add `CONNECTED→recovery` branch.
- Modify `app/engine/recovery.py` — launch `backfill_active()` on READY.
- Modify `app/engine/worker.py` — gap gate on entries in rule-driven + multi-position loops.

---

### Task 1: Gap gate + interval map (pure)

**Files:**
- Create: `backend/app/services/backfill.py`
- Test: `backend/tests/test_gap_gate.py`

**Interfaces:**
- Produces: `interval_seconds(interval: str) -> int` (`"1m"`→60, `"5m"`→300, `"15m"`→900, `"1h"`→3600, `"1d"`→86400; unknown → 300); `is_stale(bars: list[dict], interval_seconds: int, now_ms: int, *, market_open: bool) -> bool` — when `market_open` is False return False; else return True if `bars` is empty or the last bar's `time` (ms) is older than `1.5 * interval_seconds * 1000` before `now_ms`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gap_gate.py
"""Gap gate: stale only when market open and the latest bar is missing.
   Run: python -m tests.test_gap_gate
"""
from __future__ import annotations


def _run() -> None:
    from app.services.backfill import interval_seconds, is_stale

    assert interval_seconds("5m") == 300 and interval_seconds("1d") == 86400
    assert interval_seconds("weird") == 300
    now = 1_700_000_000_000
    fresh = [{"time": now - 60_000}]          # 1 min old, 5m interval
    old = [{"time": now - 600_000}]           # 10 min old > 1.5*300s

    assert is_stale([], 300, now, market_open=True) is True       # no bars
    assert is_stale(old, 300, now, market_open=True) is True      # gap
    assert is_stale(fresh, 300, now, market_open=True) is False   # fresh
    assert is_stale(old, 300, now, market_open=False) is False    # closed → never stale
    print("PASS: gap gate evaluates staleness")
    print("\nALL GAP-GATE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_gap_gate`
Expected: FAIL — `ModuleNotFoundError: app.services.backfill`.

- [ ] **Step 3: Implement**

```python
# backend/app/services/backfill.py
"""Market-data continuity: a pure gap gate + a best-effort proactive backfill that
force-refreshes active strategies' bars after a reconnect (reuses the IBKR historical
provider + history_cache)."""
from __future__ import annotations

import logging

logger = logging.getLogger("trading.backfill")

_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def interval_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(interval, 300)


def is_stale(bars: list[dict], interval_seconds: int, now_ms: int, *, market_open: bool) -> bool:
    if not market_open:
        return False
    if not bars:
        return True
    last_ms = bars[-1].get("time") or 0
    return (now_ms - last_ms) > int(1.5 * interval_seconds * 1000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_gap_gate`
Expected: `ALL GAP-GATE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/backfill.py backend/tests/test_gap_gate.py
git commit -m "feat(backfill): pure gap gate + interval map"
```

---

### Task 2: Strategy resilience hooks (`backfill_specs`, `bar_interval`)

**Files:**
- Modify: `backend/app/strategies/base.py`
- Modify: `backend/app/strategies/ema_scalper/strategy.py`
- Modify: `backend/app/strategies/ema_crossover/strategy.py`
- Test: `backend/tests/test_backfill_specs.py`

**Interfaces:**
- Produces: `BaseStrategy.backfill_specs(selected_symbol: str | None) -> list[tuple[str, str]]`
  (default `[]`) and `BaseStrategy.bar_interval() -> str` (default `"5m"`). Scalper overrides:
  `bar_interval()` → `self.params.candle_interval`; `backfill_specs(sel)` → `[(sel, self.params.candle_interval)]` when `sel` else `[]`. Crossover overrides: `bar_interval()` → `"5m"`; `backfill_specs(_)` → `[(s, "5m") for s in <watchlist>]`.

> **FIRST read** `app/strategies/base.py` (the `BaseStrategy` ABC, ~line 27) and the two strategy
> classes to confirm where `self.params` and the watchlist come from (the crossover reads
> `get_settings().watchlist_symbols`; match the exact attribute it already uses for its watchlist).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_backfill_specs.py
"""Strategy hooks expose (symbol, interval) specs + bar interval.
   Run: python -m tests.test_backfill_specs
"""
from __future__ import annotations


def _run() -> None:
    from app.strategies.base import BaseStrategy

    # default base behaviour
    class Bare(BaseStrategy):
        def __init__(self): super().__init__("k", "n")
        async def scan(self, universe): return []
    b = Bare()
    assert b.backfill_specs("AAPL") == [] and b.bar_interval() == "5m"

    from app.strategies.ema_scalper.strategy import EmaScalperStrategy
    s = EmaScalperStrategy()
    assert s.bar_interval() == s.params.candle_interval
    specs = s.backfill_specs("AAPL")
    assert specs == [("AAPL", s.params.candle_interval)], specs
    assert s.backfill_specs(None) == []
    print("PASS: strategy resilience hooks")
    print("\nALL BACKFILL-SPECS TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_backfill_specs`
Expected: FAIL — `AttributeError: 'Bare' object has no attribute 'backfill_specs'`.

- [ ] **Step 3: Implement**

In `app/strategies/base.py`, add to the `BaseStrategy` class body (alongside other defaults):

```python
    def bar_interval(self) -> str:
        """Candle interval the live loop acts on (for the data gap gate)."""
        return "5m"

    def backfill_specs(self, selected_symbol: str | None) -> list[tuple[str, str]]:
        """(symbol, interval) pairs to force-refresh after a reconnect. Default: none."""
        return []
```

In `ema_scalper/strategy.py` (the `EmaScalperStrategy` class), add:

```python
    def bar_interval(self) -> str:
        return self.params.candle_interval

    def backfill_specs(self, selected_symbol: str | None) -> list[tuple[str, str]]:
        return [(selected_symbol, self.params.candle_interval)] if selected_symbol else []
```

In `ema_crossover/strategy.py` (the crossover class), add (use the SAME watchlist source the
class already uses for its live loop — confirm in Step 0's read; shown here as
`get_settings().watchlist_symbols`):

```python
    def bar_interval(self) -> str:
        return "5m"

    def backfill_specs(self, selected_symbol: str | None) -> list[tuple[str, str]]:
        from app.config import get_settings
        return [(s, "5m") for s in get_settings().watchlist_symbols]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_backfill_specs`
Expected: `ALL BACKFILL-SPECS TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/base.py backend/app/strategies/ema_scalper/strategy.py backend/app/strategies/ema_crossover/strategy.py backend/tests/test_backfill_specs.py
git commit -m "feat(strategies): additive backfill_specs + bar_interval hooks"
```

---

### Task 3: `backfill_active()` — proactive force-refresh

**Files:**
- Modify: `backend/app/services/backfill.py`
- Test: `backend/tests/test_backfill_active.py`

**Interfaces:**
- Consumes: `get_historical_provider()`, `history_cache.save`, `registry` (`get_strategy`),
  `StrategyState`, `backfill_specs` (Task 2).
- Produces: `async backfill_active(delay: float = 0.2) -> int` — for each `StrategyState` with
  `status in ("scanning","active")`, look up the strategy via the registry, call
  `strat.backfill_specs(state.selected_symbol)`, and for each `(symbol, interval)` force-refresh:
  `bars = await provider.get_bars(symbol, interval)` then `history_cache.save(symbol, interval, bars, source=provider.name)`, sleeping `delay` between fetches. Returns the count of specs refreshed. Per-spec errors are logged and skipped.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_backfill_active.py
"""backfill_active force-refreshes active strategies' specs via provider + cache.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_bf.db python -m tests.test_backfill_active
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import StrategyState
        from app.services import backfill
        from app.services import history_cache

        await init_db()
        async with session_scope() as s:
            s.add(StrategyState(key="strategy_1", name="Scalper", ready=True,
                                status="active", selected_symbol="AAPL"))

        calls = []

        class FakeProvider:
            name = "ibkr"
            async def get_bars(self, symbol, interval, start=None, end=None):
                calls.append((symbol, interval))
                return [{"time": 1, "close": 100}]

        class FakeStrat:
            key = "strategy_1"
            def backfill_specs(self, sel):
                return [(sel, "5m")] if sel else []

        saved = []
        backfill.get_historical_provider = lambda: FakeProvider()      # type: ignore[attr-defined]
        backfill.get_strategy = lambda key: FakeStrat()                # type: ignore[attr-defined]
        history_cache.save = lambda *a, **k: saved.append((a, k))      # type: ignore[assignment]

        n = await backfill.backfill_active(delay=0)
        assert n == 1 and calls == [("AAPL", "5m")], (n, calls)
        assert saved and saved[0][0][0] == "AAPL", saved
        print("PASS: backfill_active refreshed the active spec")
    asyncio.run(_t())
    print("\nALL BACKFILL-ACTIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_bf.db ../.venv/bin/python -m tests.test_backfill_active`
Expected: FAIL — `AttributeError: module 'app.services.backfill' has no attribute 'backfill_active'`.

- [ ] **Step 3: Implement**

Append to `app/services/backfill.py` (add the imports at the top of the file):

```python
import asyncio

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import StrategyState
from app.providers import get_historical_provider
from app.services import history_cache
from app.strategies.registry import get_strategy


async def backfill_active(delay: float = 0.2) -> int:
    provider = get_historical_provider()
    refreshed = 0
    async with session_scope() as s:
        rows = (await s.execute(select(StrategyState).where(
            StrategyState.status.in_(("scanning", "active"))))).scalars().all()
        active = [(r.key, r.selected_symbol) for r in rows]
    for key, selected in active:
        strat = get_strategy(key)
        if strat is None:
            continue
        for symbol, interval in strat.backfill_specs(selected):
            try:
                bars = await provider.get_bars(symbol, interval)
                history_cache.save(symbol, interval, bars, source=provider.name)
                refreshed += 1
            except Exception as exc:  # noqa: BLE001 — best-effort per spec
                logger.warning("backfill %s/%s failed: %r", symbol, interval, exc)
            if delay:
                await asyncio.sleep(delay)
    return refreshed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_bf.db ../.venv/bin/python -m tests.test_backfill_active && rm -f _t_bf.db`
Expected: `ALL BACKFILL-ACTIVE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/backfill.py backend/tests/test_backfill_active.py
git commit -m "feat(backfill): proactive backfill_active force-refresh on reconnect"
```

---

### Task 4: Supervisor seam fix (`CONNECTED → READY`)

**Files:**
- Modify: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_supervise_seam.py`

**Interfaces:**
- Consumes: `run_recovery`, `reconnect_loop`, `heartbeat_once`, `ConnState`.
- Produces: `async Gateway._supervise_tick() -> None` — `DISCONNECTED` → `reconnect_loop()` then `run_recovery`; **connected AND `state is CONNECTED`** → `run_recovery` (drives to READY/SAFE_MODE); else connected → `heartbeat_once()`. `supervise()` loops `await asyncio.sleep(interval); await self._supervise_tick()`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_supervise_seam.py
"""A connected socket stuck in CONNECTED is driven to READY by one supervisor tick.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_seam.db python -m tests.test_supervise_seam
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()

        class CleanIB:
            def isConnected(self): return True
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        gw._ib = CleanIB()
        gw.set_state(ConnState.CONNECTED)   # reconnected, but recovery never ran
        await gw._supervise_tick()
        assert gw.state is ConnState.READY, gw.state
        print("PASS: supervisor drives CONNECTED -> READY")
    asyncio.run(_t())
    print("\nALL SUPERVISE-SEAM TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_seam.db ../.venv/bin/python -m tests.test_supervise_seam`
Expected: FAIL — `AttributeError: ... '_supervise_tick'` (or state stays CONNECTED).

- [ ] **Step 3: Implement**

In `app/ibkr/gateway.py`, replace the body of `supervise` and add `_supervise_tick`:

```python
    async def supervise(self, interval: float = 15.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._supervise_tick()

    async def _supervise_tick(self) -> None:
        from app.engine.recovery import run_recovery  # lazy: avoid import cycle
        if self.state is ConnState.DISCONNECTED:
            await self.reconnect_loop()
            await run_recovery(self._ib, self)
        elif self.is_connected() and self.state is ConnState.CONNECTED:
            await run_recovery(self._ib, self)        # reconnected without recovery → drive to READY
        elif self.is_connected():
            await self.heartbeat_once()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_seam.db ../.venv/bin/python -m tests.test_supervise_seam && rm -f _t_seam.db`
Expected: `ALL SUPERVISE-SEAM TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/tests/test_supervise_seam.py
git commit -m "feat(ibkr): supervisor drives CONNECTED->READY (reconnect seam fix)"
```

---

### Task 5: Recovery launches backfill (non-blocking)

**Files:**
- Modify: `backend/app/engine/recovery.py`
- Test: `backend/tests/test_recovery_backfill.py`

**Interfaces:**
- Consumes: `backfill_active` (Task 3).
- Produces: on a clean recovery (after setting READY), `run_recovery` calls
  `_schedule_backfill()` which does `asyncio.create_task(backfill_active())` — non-blocking, so
  READY is not delayed. `_schedule_backfill` is a module-level function (monkeypatchable in tests).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_recovery_backfill.py
"""Clean recovery schedules a backfill task (non-blocking).
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rb.db python -m tests.test_recovery_backfill
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine import recovery
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()
        scheduled = {"n": 0}
        recovery._schedule_backfill = lambda: scheduled.__setitem__("n", scheduled["n"] + 1)  # type: ignore[attr-defined]

        class CleanIB:
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        ok = await recovery.run_recovery(CleanIB(), gw)
        assert ok is True and gw.state is ConnState.READY
        assert scheduled["n"] == 1, scheduled
        print("PASS: clean recovery scheduled backfill")
    asyncio.run(_t())
    print("\nALL RECOVERY-BACKFILL TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_rb.db ../.venv/bin/python -m tests.test_recovery_backfill`
Expected: FAIL — `AttributeError: ... '_schedule_backfill'`.

- [ ] **Step 3: Implement**

In `app/engine/recovery.py`, add a module-level helper and call it on the clean path:

```python
def _schedule_backfill() -> None:
    import asyncio

    from app.services.backfill import backfill_active
    asyncio.create_task(backfill_active())
```

In `run_recovery`, after `gateway.set_state(ConnState.READY)` and before/after the clean log line,
add:

```python
    _schedule_backfill()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_rb.db ../.venv/bin/python -m tests.test_recovery_backfill && rm -f _t_rb.db`
Expected: `ALL RECOVERY-BACKFILL TESTS PASSED`.

**Regression:** re-run `DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_recovery_sequence && rm -f _t.db` — still passes (it doesn't monkeypatch `_schedule_backfill`, so the real `create_task` runs; that is safe because `backfill_active` reads an empty active set in that test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/recovery.py backend/tests/test_recovery_backfill.py
git commit -m "feat(engine): recovery schedules proactive backfill on READY"
```

---

### Task 6: Worker gap gate on entries

**Files:**
- Modify: `backend/app/engine/worker.py`
- Test: `backend/tests/test_worker_gap_gate.py`

**Interfaces:**
- Consumes: `is_stale`, `interval_seconds` (Task 1); `bar_interval` (Task 2);
  `get_market_status` (`app.services.market`).
- Produces: helper `async _entry_blocked_by_gap(strategy, bars) -> bool` returning True when the
  market is open and `bars` is stale for `strategy.bar_interval()`. Rule-driven loop: when the
  action is an opening action (`arm`/`enter`) and `_entry_blocked_by_gap` is True, skip acting
  (log at debug). Multi-position loop: gate only the `enter` branch. Exits/cancels are never gated.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_worker_gap_gate.py
"""Entries are blocked on a stale series when the market is open; fresh bars are not.
   Run: python -m tests.test_worker_gap_gate
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.engine import worker as W

        class Strat:
            key = "strategy_1"
            def bar_interval(self): return "5m"

        now_ms = __import__("time").time() * 1000
        stale_bars = [{"time": int(now_ms - 3_600_000)}]   # 1h old
        fresh_bars = [{"time": int(now_ms - 30_000)}]      # 30s old

        # Force market open for the test.
        import app.services.market as mkt
        mkt.get_market_status = lambda: {"is_open": True}   # type: ignore[assignment]

        assert await W._entry_blocked_by_gap(Strat(), stale_bars) is True
        assert await W._entry_blocked_by_gap(Strat(), fresh_bars) is False

        mkt.get_market_status = lambda: {"is_open": False}  # type: ignore[assignment]
        assert await W._entry_blocked_by_gap(Strat(), stale_bars) is False  # closed → allowed
        print("PASS: entry gap gate")
    asyncio.run(_t())
    print("\nALL WORKER-GAP-GATE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_worker_gap_gate`
Expected: FAIL — `AttributeError: ... '_entry_blocked_by_gap'`.

- [ ] **Step 3: Implement**

Add the helper near the top of `worker.py` (after `_trading_allowed`):

```python
async def _entry_blocked_by_gap(strategy, bars) -> bool:
    """True when the market is open and the symbol's series is missing its latest bar."""
    import time

    from app.services.backfill import interval_seconds, is_stale
    from app.services.market import get_market_status
    secs = interval_seconds(strategy.bar_interval())
    return is_stale(bars, secs, int(time.time() * 1000),
                    market_open=get_market_status()["is_open"])
```

Wire it in **`_handle_action`-callers**, not inside `_handle_action`:

- Rule-driven loop (`_run_rule_driven`): where it currently guards
  `if action is not None and action.kind != "none" and cur_ms != last_bar_ms:`, change to also
  require, for opening actions, that the gap gate is clear:

```python
                    blocked = (action is not None and action.kind in ("arm", "enter")
                               and await _entry_blocked_by_gap(strategy, bars))
                    if blocked:
                        logger.debug("entry gated (awaiting backfill) %s", selected)
                    if (action is not None and action.kind != "none"
                            and cur_ms != last_bar_ms and not blocked):
                        last_bar_ms = cur_ms
                        open_qty, open_trade_id = await _handle_action(
                            strategy, selected, action, state, bars,
                            open_qty, open_trade_id,
                            size_position, submit_equity_order, apply_action,
                        )
```

- Multi-position loop (`_run_multi_position`): in the flat→entry branch, add the gate:

```python
                        if (action and action.kind == "enter"
                                and cur_ms != last_entry_ms.get(symbol)
                                and not await _entry_blocked_by_gap(strategy, bars)):
                            last_entry_ms[symbol] = cur_ms
                            t = await _open_bracket(strategy, symbol, action, state, bars, engine)
                            if t:
                                track[symbol] = t
```

> **FIRST read** the current `_run_rule_driven` (≈ lines 70–115) and `_run_multi_position`
> (≈ lines 200–245) blocks and splice the gate in to match their exact structure; do not change
> the exit/cancel paths.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_worker_gap_gate`
Expected: `ALL WORKER-GAP-GATE TESTS PASSED`.

**Regression (shared file `worker.py`):** re-run `test_engine_worker`, `test_worker_pause`,
`test_mp_worker`, `test_mp_multi`, `test_worker_state_writes`, `test_worker_state_rebuild`
(each with a throwaway `DATABASE_URL`) — all still pass. Note `test_mp_worker`/`test_mp_multi`
feed bars with old timestamps; they assert entries happen, so ensure those tests run with the
market gate satisfied — they already monkeypatch nothing for market, so if they now fail due to
the gate, set `app.services.market.get_market_status` to `lambda: {"is_open": False}` at the top
of those tests' `_run()` (closed → entries allowed), matching how they exercise entry logic
deterministically.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/worker.py backend/tests/test_worker_gap_gate.py backend/tests/test_mp_worker.py backend/tests/test_mp_multi.py
git commit -m "feat(engine): gap gate blocks entries on a stale series (exits never gated)"
```

---

## Manual smoke (live, after the suite is green)

With the paper IB Gateway connected during RTH, with a strategy active: disconnect the Gateway
mid-session for a minute, then reconnect. Confirm the logs show reconnect → recovery → READY →
`backfill_active` refreshing the active symbol's bars, and that the worker resumes opening
positions only once its series is contiguous (no entry on the gap).

## Self-review notes
- Spec §1 gap gate → Task 1. §2 backfill → Tasks 2–3. §3 seam → Task 4. §4 recovery trigger →
  Task 5. §5 worker integration → Task 6. Testing §all → each task's tests + manual smoke.
- `interval_seconds`/`is_stale`/`backfill_active`/`backfill_specs`/`bar_interval`/`_supervise_tick`/
  `_schedule_backfill`/`_entry_blocked_by_gap` names are consistent across tasks.
