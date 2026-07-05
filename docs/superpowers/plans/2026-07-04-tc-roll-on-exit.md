# Triple Calendar Roll-on-Exit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the Triple Calendar hits its +10% profit target or −25% stop, close the position (realizing P&L) and immediately re-enter a fresh three-calendar structure at the current level (gated by VIX ≤ 16 / events, fail-closed), with a 5s VIX fast-poll while flat and waiting for VIX to fall.

**Architecture:** The pure `manage()` re-labels both exits as a new `"roll"` action (behind a `roll_on_exit` flag). `manage_step()` executes the roll: SELL to close, then `decide_entry` at the current level → BUY if allowed. `_persist()` closes the old DB record and saves the new one. `run_live()` fetches VIX every tick, passes it in, and picks a 5s vs 300s sleep based on whether it's flat and VIX-blocked.

**Tech Stack:** Python 3.14, async SQLAlchemy (SQLite), ib_async, pytest.

## Global Constraints

- **Run Python from `backend/`** with `../.venv/bin/python`. DB-touching tests set `DATABASE_URL=sqlite+aiosqlite:///./_t_<name>.db` and `await init_db()` first.
- **Branch:** `tc-roll-on-exit` — commit there; do NOT create/switch branches.
- **No roll cap** — the VIX ≤ 16 entry gate is the only backstop (intentional).
- **Fail-closed:** never open OR reopen a position without a VIX reading. `decide_entry` stays permissive on `vix=None` (for backtest/unit tests); the live layer enforces fail-closed — for entry in `run_live`, for roll re-entry inside `manage_step`.
- **`roll_on_exit` default True** (the behavior the user asked for); `False` restores today's plain exit.
- Exit ordering in `manage()` is unchanged: profit target first, then stop, then breach, then hold.
- Style: `from __future__ import annotations`, existing module docstrings, `log_event(..., category="trade")` for trade events.

---

### Task 1: Config knobs — `roll_on_exit` param + `vix_wait_poll_seconds` setting

**Files:**
- Modify: `backend/app/strategies/triple_calendar/params.py` (after the `vix_entry_max` line)
- Modify: `backend/app/config.py` (after `builder_monitor_interval_seconds`)
- Test: `backend/tests/test_tc_params.py` (extend), `backend/tests/test_builder_config.py` (extend)

**Interfaces:**
- Produces: `TripleCalendarParams.roll_on_exit: bool = True`; `Settings.vix_wait_poll_seconds: int = 5`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_tc_params.py` inside `test_defaults` (add the assertion after the existing `vix_entry_max` line):

```python
    assert p.roll_on_exit is True
```

Add to `backend/tests/test_builder_config.py`:

```python
def test_vix_wait_poll_default():
    from app.config import get_settings
    assert get_settings().vix_wait_poll_seconds == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_params.py tests/test_builder_config.py -q`
Expected: FAIL — `AttributeError` for `roll_on_exit` and `vix_wait_poll_seconds`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/params.py`, directly below the `vix_entry_max` line:

```python
    roll_on_exit: bool = True        # +10% target / -25% stop close AND immediately reopen at
    #                                  the current level (vs. plain exit) when True
```

In `backend/app/config.py`, directly below `builder_monitor_interval_seconds: int = 60`:

```python
    vix_wait_poll_seconds: int = 5   # while flat + VIX > entry ceiling, re-check VIX this often
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_params.py tests/test_builder_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/params.py backend/app/config.py backend/tests/test_tc_params.py backend/tests/test_builder_config.py
git commit -m "feat(triple_calendar): roll_on_exit param + vix_wait_poll_seconds setting"
```

---

### Task 2: `manage()` labels both exits as `roll` (behind the flag)

**Files:**
- Modify: `backend/app/strategies/triple_calendar/strategy.py` (the `manage()` function, target/stop branches)
- Modify: `backend/tests/test_tc_exit.py` (pin classic behavior to `roll_on_exit=False`)
- Test: `backend/tests/test_tc_roll.py` (create — pure `manage()` cases)

**Interfaces:**
- Consumes: `TripleCalendarParams.roll_on_exit` (Task 1).
- Produces: `manage(position, day, source, params)` returns `("roll", {"reason": "target"|"stop_loss", "value": float, "pnl_pct": float})` at +10%/−25% when `params.roll_on_exit` is true; returns `("exit", {...})` (as today) when false. Return tuple shape otherwise unchanged.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_tc_roll.py`:

```python
"""Triple Calendar roll-on-exit: manage() labels +10%/-25% as 'roll' when roll_on_exit is on;
manage_step closes then reopens at the current level (fail-closed on VIX).
   Run: python -m tests.test_tc_roll
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.strategy import manage

DAY = date(2026, 1, 1)
SELL = DAY + timedelta(days=21)
BUY = DAY + timedelta(days=28)


class _Src:
    """Calendar value = buy - sell = `value`; underlying_close None to skip breach."""
    def __init__(self, value):
        self.value = value
    def option_close(self, u, expiry, strike, right, d):
        return self.value if expiry == BUY else 0.0
    def underlying_close(self, u, d):
        return None


def _pos():
    return Position(underlying="QQQ", entry_date=DAY,
                    calendars=[Calendar(725.0, SELL, BUY)], net_debit=10.0)


def test_manage_rolls_on_target_and_stop_when_enabled():
    p = TripleCalendarParams()  # roll_on_exit defaults True
    act, info = manage(_pos(), DAY, _Src(11.0), p)      # +10%
    assert act == "roll" and info["reason"] == "target"
    act, info = manage(_pos(), DAY, _Src(7.5), p)       # -25%
    assert act == "roll" and info["reason"] == "stop_loss"


def test_manage_exits_when_roll_disabled():
    p = TripleCalendarParams(roll_on_exit=False)
    act, info = manage(_pos(), DAY, _Src(11.0), p)
    assert act == "exit" and info["reason"] == "target"
    act, info = manage(_pos(), DAY, _Src(7.5), p)
    assert act == "exit" and info["reason"] == "stop_loss"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_roll.py -q`
Expected: FAIL — `test_manage_rolls_on_target_and_stop_when_enabled` asserts `"roll"` but current code returns `"exit"`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/strategy.py`, in `manage()`, replace the target/stop block:

```python
    value = position_value(source, position, day)
    if value is not None and position.net_debit > 0:
        pnl_pct = (value - position.net_debit) / position.net_debit
        if pnl_pct >= params.profit_target:
            return "exit", {"reason": "target", "value": value, "pnl_pct": pnl_pct}
        if pnl_pct <= -params.stop_loss_pct:
            return "exit", {"reason": "stop_loss", "value": value, "pnl_pct": pnl_pct}
```

with:

```python
    value = position_value(source, position, day)
    if value is not None and position.net_debit > 0:
        pnl_pct = (value - position.net_debit) / position.net_debit
        # +10% target / -25% stop: roll (close + reopen at current level) when enabled,
        # else a plain exit. manage() only labels it; manage_step does the close/reopen.
        exit_action = "roll" if params.roll_on_exit else "exit"
        if pnl_pct >= params.profit_target:
            return exit_action, {"reason": "target", "value": value, "pnl_pct": pnl_pct}
        if pnl_pct <= -params.stop_loss_pct:
            return exit_action, {"reason": "stop_loss", "value": value, "pnl_pct": pnl_pct}
```

Then pin the classic test: in `backend/tests/test_tc_exit.py`, change the params line
`params = TripleCalendarParams()  # profit_target 0.10, stop_loss_pct 0.25` to:

```python
    params = TripleCalendarParams(roll_on_exit=False)  # classic exits: profit 0.10, stop 0.25
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_roll.py -q && ../.venv/bin/python -m tests.test_tc_exit`
Expected: PASS (roll tests pass; `ALL TC-EXIT TESTS PASSED`).

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/strategy.py backend/tests/test_tc_roll.py backend/tests/test_tc_exit.py
git commit -m "feat(triple_calendar): manage() labels +10%/-25% as roll when roll_on_exit"
```

---

### Task 3: `manage_step()` roll branch — close then reopen (fail-closed)

**Files:**
- Modify: `backend/app/strategies/triple_calendar/live.py` (`manage_step`, add a `"roll"` branch)
- Test: `backend/tests/test_tc_roll.py` (extend)

**Interfaces:**
- Consumes: `manage()` returning `"roll"` (Task 2); `decide_entry`, `entry_legs`, `exit_legs`, `position_value` (already imported/defined in `live.py`).
- Produces: `manage_step(..., vix=...)` handling `act == "roll"`: awaits a SELL close then, if `vix is not None` and `decide_entry` returns a position, a BUY re-entry. Returns `{"action": "roll", "position": <new pos | None>, "info": {"reason", "closed_pnl", "reopened": bool}}`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_tc_roll.py`:

```python
class _EntrySrc:
    """Supports decide_entry (spot/chain/option_close) AND management valuation. The open
    calendar is worth `value`; fresh entries build from the same rising-with-DTE curve."""
    def __init__(self, value, spot=445.0):
        self.value, self.spot = value, spot
        self.strikes = [float(r) for r in range(400, 491, 5)]
    def underlying_close(self, u, d):
        return self.spot
    def chain_strikes(self, u, e):
        return self.strikes
    def option_close(self, u, expiry, strike, right, d):
        # The seeded position's strike (725) values cleanly: buy leg -> `value`, sell leg -> 0,
        # so its calendar value == `value`. Fresh decide_entry uses strikes near ATM (~445),
        # which fall to the rising-with-DTE curve -> a positive-debit structure.
        if strike == 725.0:
            return self.value if expiry == BUY else 0.0
        return 1.0 + 0.1 * max((expiry - d).days, 0)


class _Exec:
    def __init__(self):
        self.calls = []
    async def __call__(self, underlying, legs, **kw):
        self.calls.append((kw.get("reason"), kw.get("action"), len(legs)))
        return {"transmitted": False, "paper": True, "order_id": None}


@pytest.mark.asyncio
async def test_manage_step_roll_reopens_when_vix_ok():
    from app.strategies.triple_calendar.live import manage_step
    ex = _Exec()
    p = TripleCalendarParams()
    out = await manage_step(DAY, _EntrySrc(7.5), p, ex, position=_pos(), vix=12.0)  # -25% stop
    assert out["action"] == "roll" and out["info"]["reopened"] is True
    assert out["position"] is not None
    assert out["info"]["closed_pnl"] == pytest.approx(-2.5)   # 7.5 - 10.0
    reasons = [c[0] for c in ex.calls]
    assert "roll_stop_loss" in reasons and "roll_entry" in reasons   # a SELL then a BUY


@pytest.mark.asyncio
async def test_manage_step_roll_target_reopens():
    from app.strategies.triple_calendar.live import manage_step
    ex = _Exec()
    out = await manage_step(DAY, _EntrySrc(11.0), TripleCalendarParams(), ex,
                            position=_pos(), vix=12.0)  # +10% target
    assert out["action"] == "roll" and out["info"]["reason"] == "target"
    assert out["info"]["closed_pnl"] == pytest.approx(1.0) and out["info"]["reopened"] is True


@pytest.mark.asyncio
async def test_manage_step_roll_fail_closed_no_reopen():
    from app.strategies.triple_calendar.live import manage_step
    # vix=None (unreadable) and vix>16 must both close WITHOUT reopening.
    for vix in (None, 17.0):
        ex = _Exec()
        out = await manage_step(DAY, _EntrySrc(7.5), TripleCalendarParams(), ex,
                                position=_pos(), vix=vix)
        assert out["action"] == "roll" and out["position"] is None
        assert out["info"]["reopened"] is False
        actions = [c[1] for c in ex.calls]
        assert actions == ["SELL"], f"expected close-only, got {ex.calls} (vix={vix})"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_roll.py -q`
Expected: FAIL — `manage_step` has no `"roll"` branch, so it falls through to `"hold"` (no reopen, wrong action).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/live.py`, in `manage_step`, add a `"roll"` branch immediately BEFORE the existing `if act == "exit":` block:

```python
    if act == "roll":
        reason = info.get("reason", "roll")
        value = info.get("value")
        if value is None:
            value = position_value(source, position, day) or position.net_debit
        closed_pnl = value - position.net_debit
        # Close the current position (realize P&L).
        await executor(position.underlying, exit_legs(position), net_price=None,
                       action="SELL", reason=f"roll_{reason}")
        # Reopen at the current level. Fail closed: never reopen without a VIX reading; a
        # readable VIX still passes through decide_entry's <=16 / event / debit gates.
        new_pos = decide_entry(day, source, params, events, vix) if vix is not None else None
        if new_pos is not None:
            await executor(new_pos.underlying, entry_legs(new_pos),
                           net_price=round(new_pos.net_debit, 2), action="BUY", reason="roll_entry")
            return {"action": "roll", "position": new_pos,
                    "info": {"reason": reason, "closed_pnl": closed_pnl, "reopened": True}}
        return {"action": "roll", "position": None,
                "info": {"reason": reason, "closed_pnl": closed_pnl, "reopened": False}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_roll.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/live.py backend/tests/test_tc_roll.py
git commit -m "feat(triple_calendar): manage_step roll — close then reopen at current level (fail-closed)"
```

---

### Task 4: `strategy_class` wiring — persist the roll + VIX every tick + adaptive cadence

**Files:**
- Modify: `backend/app/strategies/triple_calendar/strategy_class.py` (`_persist` roll branch; `run_live` loop)
- Test: `backend/tests/test_tc_roll_live.py` (create)

**Interfaces:**
- Consumes: `manage_step` returning `"roll"` (Task 3); `close_position`, `save_position` (already imported); `get_settings().vix_wait_poll_seconds` (Task 1); `params.vix_entry_max`.
- Produces: on a `"roll"` tick, `_persist` closes the old `OptionPositionRecord` with `closed_pnl` and (if reopened) saves the new position, returning `(new_pos, new_rid)` or `(None, None)`. `run_live` fetches VIX every tick, passes it to `manage_step`, and sleeps `vix_wait_poll_seconds` while flat + VIX-blocked (else `tc_manage_interval_seconds`).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_tc_roll_live.py`:

```python
"""The live loop persists a roll (closes the old record, opens a new one) and fast-polls VIX
while flat + waiting for VIX <= 16.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rl.db python -m tests.test_tc_roll_live
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import OptionPositionRecord
        from app.ibkr.gateway import ConnState, get_gateway
        from app.strategies.triple_calendar import live_source, strategy_class
        from app.strategies.triple_calendar.legs import Calendar, Position
        from app.strategies.triple_calendar.store import save_position
        from sqlalchemy import select

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02
        get_settings().vix_wait_poll_seconds = 0.02

        today = date.today()
        sell, buy = today + timedelta(days=21), today + timedelta(days=28)
        # Seed an open position already at a -25% loss so the first managed tick rolls it.
        pos = Position("QQQ", today, [Calendar(725.0, sell, buy)], net_debit=10.0)
        old_id = await save_position("strategy_2", pos, order_ids=[], paper=True)

        strikes = [float(r) for r in range(400, 491, 5)]

        async def a_spot(self, sym):
            return 445.0
        async def a_chain(self, u):
            return strikes
        async def a_mid(self, u, expiry, strike, right):
            # seeded 725 position: buy leg -> 7.5, sell leg -> 0, so value 7.5 on debit 10 =
            # -25% -> rolls. fresh entry strikes (~445) use the DTE curve (positive debit).
            if strike == 725.0:
                return 7.5 if expiry == buy else 0.0
            return 1.0 + 0.1 * max((expiry - today).days, 0)
        async def a_vix(self):
            return 12.0
        live_source.LivePriceSource.aunderlying = a_spot   # type: ignore[assignment]
        live_source.LivePriceSource.achain = a_chain       # type: ignore[assignment]
        live_source.LivePriceSource.aoption_mid = a_mid     # type: ignore[assignment]
        live_source.LivePriceSource.avix = a_vix            # type: ignore[assignment]
        strategy_class.get_market_status = lambda: {"is_open": True}  # type: ignore[assignment]
        get_gateway().set_state(ConnState.READY)

        strat = strategy_class.TripleCalendarStrategy()
        task = asyncio.create_task(strat.run_live())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        async with session_scope() as s:
            old = await s.get(OptionPositionRecord, old_id)
            open_rows = (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == "strategy_2",
                OptionPositionRecord.status == "open"))).scalars().all()
        assert old.status == "closed", "the rolled position should be closed"
        assert old.pnl is not None and old.pnl < 0, f"closed at a loss, got {old.pnl}"
        assert len(open_rows) == 1 and open_rows[0].id != old_id, "a fresh position should be open"
        print(f"PASS: roll persisted — old #{old_id} closed pnl={old.pnl:.2f}, new #{open_rows[0].id} open")

    asyncio.run(_t())
    print("\nALL TC-ROLL-LIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_rl.db && DATABASE_URL=sqlite+aiosqlite:///./_t_rl.db ../.venv/bin/python -m tests.test_tc_roll_live 2>&1 | tail -5; rm -f _t_rl.db`
Expected: FAIL — `_persist` has no roll branch, so the old record isn't closed / no new record opens (assertion error).

- [ ] **Step 3: Write minimal implementation**

**3a — `_persist` roll branch.** In `backend/app/strategies/triple_calendar/strategy_class.py`, in `_persist`, add BEFORE the `if action == "exit" and record_id is not None:` block:

```python
        if action == "roll" and record_id is not None:
            await close_position(record_id, pnl=out["info"].get("closed_pnl", 0.0))
            reason = out["info"].get("reason", "roll")
            await log_event(f"ROLL close ({reason}) pnl {out['info'].get('closed_pnl', 0.0):.2f}",
                            category="trade", strategy_key=self.key)
            new_pos = out["position"]
            if new_pos is not None:
                rid = await save_position(self.key, new_pos, order_ids=[],
                                          paper=not get_settings().enable_live_orders)
                await log_event(f"ROLL reopen {new_pos.underlying} debit {new_pos.net_debit:.2f}",
                                category="trade", strategy_key=self.key)
                return new_pos, rid
            return None, None
```

**3b — `run_live` loop: VIX every tick + fail-closed flat + adaptive sleep.** Replace the whole loop body (from `while True:` through the final `await asyncio.sleep(...)`) with:

```python
        while True:
            # Pause unless READY AND the regular session is open — no trading on stale quotes.
            if get_gateway().state is not ConnState.READY or not get_market_status()["is_open"]:
                await asyncio.sleep(get_settings().tc_manage_interval_seconds)
                continue
            vix = None
            try:
                today = datetime.now().date()
                spot = await src.aunderlying(params.underlying)
                if spot is not None:
                    # Fetch VIX every tick: a holding tick may roll and must gate its re-entry.
                    vix = await src.avix()
                    # Flat entry is fail-closed: no VIX reading -> don't enter this tick.
                    if position is None and vix is None:
                        await log_event("Triple Calendar: skipping entry — VIX unavailable "
                                        "(fail-closed)", category="decision", strategy_key=self.key)
                    else:
                        strikes = await src.achain(params.underlying)
                        snapshot = await _resolve_snapshot(src, params, spot, strikes, today, position)
                        out = await manage_step(today, snapshot, params, executor, position, vix=vix)
                        position, record_id = await self._persist(out, record_id)
                        # Persist the live valuation the core computed (in-memory snapshot).
                        if position is not None and record_id is not None and position.net_debit:
                            from app.strategies.triple_calendar.strategy import position_value
                            val = position_value(snapshot, position, today)
                            if val is not None:
                                await update_value(record_id, val,
                                                   (val - position.net_debit) / position.net_debit, spot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await log_event("Triple Calendar tick error", level="warning",
                                category="decision", strategy_key=self.key)
            # Adaptive cadence: fast-poll VIX while flat + above the entry ceiling (or unreadable),
            # otherwise the normal multi-day manage interval.
            poll = get_settings().tc_manage_interval_seconds
            if position is None and (vix is None or vix > params.vix_entry_max):
                poll = get_settings().vix_wait_poll_seconds
            await asyncio.sleep(poll)
```

(Note: after a roll that reopens, `snapshot` was resolved for the old position, so the new position's legs aren't in it — `position_value` returns `None` and the value block skips this tick, valuing it next tick. Expected.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_rl.db && DATABASE_URL=sqlite+aiosqlite:///./_t_rl.db ../.venv/bin/python -m tests.test_tc_roll_live 2>&1 | tail -3; rm -f _t_rl.db`
Expected: `ALL TC-ROLL-LIVE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/strategy_class.py backend/tests/test_tc_roll_live.py
git commit -m "feat(triple_calendar): persist rolls + fetch VIX every tick + 5s VIX-wait cadence"
```

---

### Task 5: Metadata + full regression

**Files:**
- Modify: `backend/app/strategies/triple_calendar/strategy_class.py` (`rules`, `exit_logic` metadata)

- [ ] **Step 1: Update the displayed strategy metadata**

In `backend/app/strategies/triple_calendar/strategy_class.py`, replace the `rules` list's last entry and `exit_logic`:

Change the rule:
```python
             "Close at +10% of net debit or -25% stop-loss; add calendars on a strike breach"]
```
to:
```python
             "At +10% target or -25% stop: roll — close and reopen at the current level (VIX ≤ 16)",
             "Add calendars on a strike breach"]
```

Change:
```python
    exit_logic = "10% debit target / -25% stop-loss / breach adjustments."
```
to:
```python
    exit_logic = "+10% target / -25% stop both roll into a fresh calendar at the current level (VIX ≤ 16); breach adds a calendar."
```

- [ ] **Step 2: Run the full Triple Calendar regression**

Run:
```bash
cd backend
../.venv/bin/python -c "import app.main; print('main imports OK')"
rm -f _t_r.db
DATABASE_URL=sqlite+aiosqlite:///./_t_r.db ../.venv/bin/python -m pytest \
  tests/test_tc_params.py tests/test_tc_strategy.py tests/test_tc_manage_step.py \
  tests/test_tc_roll.py tests/test_tc_strategy_class.py tests/test_tc_adjust.py \
  tests/test_builder_config.py -q
rm -f _t_r.db
for t in test_tc_exit test_tc_ready_gate test_tc_vix_live test_tc_live_persist test_tc_roll_live; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -E "PASSED|Error|assert" | tail -1
  rm -f _t_$t.db
done
```
Expected: all pytest tests pass; each standalone test prints its `... PASSED` line. In particular `test_tc_exit` (now `roll_on_exit=False`), `test_tc_vix_live`, and `test_tc_live_persist` still pass (no regressions from the loop rewrite).

- [ ] **Step 3: Commit**

```bash
git add backend/app/strategies/triple_calendar/strategy_class.py
git commit -m "docs(triple_calendar): metadata reflects roll-on-exit behavior"
```

---

## Self-review notes

- **Spec coverage:** params `roll_on_exit` (T1) + `vix_wait_poll_seconds` as a setting (T1, refined from spec's params.py — it's a loop cadence read via `get_settings()` like `tc_manage_interval_seconds`); `manage()` roll labeling both exits + flag (T2); `manage_step` close+reopen fail-closed (T3); `_persist` roll + VIX-every-tick + adaptive 5s/300s cadence (T4); metadata (T5); tests incl. `roll_on_exit=False` preserving classic exits and fail-closed reopen (T2–T4). All covered.
- **No roll cap:** honored — nothing counts or limits rolls.
- **Type consistency:** `manage()` → `("roll", {reason, value, pnl_pct})`; `manage_step` roll → `{"action":"roll","position",...,"info":{reason, closed_pnl, reopened}}`; `_persist` reads `out["info"]["closed_pnl"]`/`["reason"]` and `out["position"]`. Consistent across T2–T4.
- **Placeholder scan:** none.
