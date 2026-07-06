# Contracts (Quantity) Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose how many contracts (whole multiples of the structure) to trade — a "Contracts" setting on the Triple Calendar (scales the combo order quantity + P&L) and a "Contracts" multiplier on Builder saved trades (scales tracked P&L).

**Architecture:** `contracts` is an integer ≥ 1 whole-structure multiplier. Net debit stays per-1x; contracts scales the order quantity (TC only) and dollar P&L (`(value − net_debit) × 100 × contracts`). New DB columns on `strategy_state`, `option_positions`, `builder_positions` (additive migrations); the TC live loop threads the setting into the order and persists it per position; a PUT endpoint sets it; the options-position and monitor P&L multiply by it; two small frontend inputs.

**Tech Stack:** Python 3.14, FastAPI, async SQLAlchemy (SQLite), ib_async; React + Vite.

## Global Constraints

- **Run Python from `backend/`** with `../.venv/bin/python`. Pytest style; async tests `@pytest.mark.asyncio`. DB-touching tests set `DATABASE_URL=sqlite+aiosqlite:///./_t_<name>.db` and `await init_db()`.
- **Branch:** `contracts-qty` — commit there; do NOT create/switch branches.
- **`contracts` is an integer ≥ 1**, default 1. Clamp/validate everywhere it's set.
- **P&L scaling:** dollar P&L = `(value − net_debit) × 100 × contracts`. `net_debit`, `value`, target/stop values and all percentages stay **per-1x** (unaffected).
- **Semantics (TC):** changing Contracts applies to the **next entry**; an open position keeps the contracts it was opened with (stored on its `OptionPositionRecord`); rolls re-enter at the current setting.
- **Builder no-transmit invariant** stays: the builder never places orders (contracts only scales tracked P&L there).
- Style: `from __future__ import annotations`, module docstrings.

---

### Task 1: DB columns + `Position.contracts`

**Files:**
- Modify: `backend/app/db/models.py` (`StrategyState`, `OptionPositionRecord`)
- Modify: `backend/app/db/base.py` (`_ensure_added_columns`)
- Modify: `backend/app/strategies/triple_calendar/legs.py` (`Position` dataclass)
- Test: `backend/tests/test_contracts_columns.py`

**Interfaces:**
- Produces: `StrategyState.contracts: int` (default 1), `OptionPositionRecord.contracts: int` (default 1), `Position.contracts: int = 1`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_contracts_columns.py
import pytest

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.strategies.triple_calendar.legs import Calendar, Position
from datetime import date


def test_position_dataclass_has_contracts():
    p = Position("QQQ", date(2026, 1, 1))
    assert p.contracts == 1
    p2 = Position("QQQ", date(2026, 1, 1), contracts=3)
    assert p2.contracts == 3


@pytest.mark.asyncio
async def test_contracts_columns_round_trip():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
            s.add(st)
        st.contracts = 2
        rec = OptionPositionRecord(strategy_key="strategy_2", underlying="QQQ",
                                   net_debit=4.0, contracts=5, calendars=[], order_ids=[])
        s.add(rec)
        await s.flush()
        rid = rec.id
    async with session_scope() as s:
        assert (await s.get(StrategyState, "strategy_2")).contracts == 2
        assert (await s.get(OptionPositionRecord, rid)).contracts == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_c.db && DATABASE_URL=sqlite+aiosqlite:///./_t_c.db ../.venv/bin/python -m pytest tests/test_contracts_columns.py -q; rm -f _t_c.db`
Expected: FAIL — `TypeError`/`AttributeError` (no `contracts` on `Position`/models).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/legs.py`, in the `Position` dataclass, add after `size`:
```python
    contracts: int = 1
```

In `backend/app/db/models.py`, in `StrategyState`, add after `capital_allocation`:
```python
    contracts: Mapped[int] = mapped_column(Integer, default=1)
```
In `OptionPositionRecord`, add after `net_debit`:
```python
    contracts: Mapped[int] = mapped_column(Integer, default=1)
```

In `backend/app/db/base.py`, inside `_ensure_added_columns`, in the `strategy_state` block (after the `direction` check) add:
```python
        if "contracts" not in cols:
            conn.execute(text("ALTER TABLE strategy_state ADD COLUMN contracts INTEGER DEFAULT 1"))
```
And in the `option_positions` block (after the `valued_at` check) add:
```python
        if "contracts" not in cols:
            conn.execute(text("ALTER TABLE option_positions ADD COLUMN contracts INTEGER DEFAULT 1"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_c.db && DATABASE_URL=sqlite+aiosqlite:///./_t_c.db ../.venv/bin/python -m pytest tests/test_contracts_columns.py -q; rm -f _t_c.db`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/db/base.py backend/app/strategies/triple_calendar/legs.py backend/tests/test_contracts_columns.py
git commit -m "feat(contracts): add contracts columns (strategy_state, option_positions) + Position.contracts"
```

---

### Task 2: `submit_combo_order` quantity param

**Files:**
- Modify: `backend/app/engine/execution.py` (`submit_combo_order`)
- Test: `backend/tests/test_combo_quantity.py`

**Interfaces:**
- Produces: `submit_combo_order(underlying, legs, *, net_price=None, action="BUY", reason="", quantity: int = 1)` — the placed order's total quantity equals `quantity`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_combo_quantity.py
import pytest

from app.config import get_settings
from app.engine import execution


class _FakeIB:
    def __init__(self):
        self.placed = None

    async def qualifyContractsAsync(self, c):
        c.conId = 111  # pretend it resolved
        return [c]

    def placeOrder(self, contract, order):
        self.placed = order
        class _T:  # minimal trade stub
            class order:  # noqa: N801
                orderId = 7
        _T.order.orderId = 7
        return _T


@pytest.mark.asyncio
async def test_submit_combo_order_uses_quantity(monkeypatch):
    fake = _FakeIB()

    class _GW:
        def is_connected(self): return True
        async def get_ib(self): return fake

    monkeypatch.setattr(execution, "get_gateway", lambda: _GW())
    monkeypatch.setattr(get_settings(), "enable_live_orders", True, raising=False)

    from datetime import date
    legs = [{"expiry": date(2026, 7, 17), "strike": 500.0, "right": "C", "action": "BUY"}]
    out = await execution.submit_combo_order("QQQ", legs, net_price=4.2, action="BUY",
                                             reason="entry", quantity=3)
    assert out["transmitted"] is True
    assert fake.placed.totalQuantity == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_combo_quantity.py -q`
Expected: FAIL — `TypeError: submit_combo_order() got an unexpected keyword argument 'quantity'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/engine/execution.py`, change the `submit_combo_order` signature line to add `quantity`:
```python
async def submit_combo_order(underlying: str, legs: list[dict], *, net_price: float | None = None,
                             action: str = "BUY", reason: str = "", quantity: int = 1) -> dict:
```
And change the order-construction line from:
```python
    order = LimitOrder(action, 1, net_price) if net_price is not None else MarketOrder(action, 1)
```
to:
```python
    q = max(1, int(quantity))
    order = LimitOrder(action, q, net_price) if net_price is not None else MarketOrder(action, q)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_combo_quantity.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/execution.py backend/tests/test_combo_quantity.py
git commit -m "feat(contracts): submit_combo_order honors a quantity param"
```

---

### Task 3: `manage_step` threads contracts into orders

**Files:**
- Modify: `backend/app/strategies/triple_calendar/live.py` (`manage_step`)
- Test: `backend/tests/test_tc_contracts_step.py`

**Interfaces:**
- Consumes: `Position.contracts` (Task 1).
- Produces: `manage_step(day, source, params, executor, position, events=None, vix=None, contracts: int = 1)`. On **entry** and **roll re-entry** it sets the new position's `.contracts = contracts` and places the order with `quantity=contracts`; on **adjust/exit/roll-close** it places with `quantity=position.contracts`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_contracts_step.py
import pytest

from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.params import TripleCalendarParams


class Src:
    def underlying_close(self, s, d): return 445.0
    def chain_strikes(self, u, e): return [float(r) for r in range(400, 491, 5)]
    def option_close(self, u, e, k, r, d): return 1.0 + 0.1 * max((e - d).days, 0)


class Exec:
    def __init__(self): self.calls = []
    async def __call__(self, underlying, legs, **kw):
        self.calls.append((kw.get("reason"), kw.get("action"), kw.get("quantity")))
        return {"transmitted": False, "paper": True}


@pytest.mark.asyncio
async def test_entry_stamps_and_orders_with_contracts():
    from datetime import date
    ex = Exec()
    out = await manage_step(date(2026, 6, 25), Src(), TripleCalendarParams(), ex,
                            position=None, contracts=4)
    assert out["action"] == "enter"
    assert out["position"].contracts == 4
    # the entry order was placed with quantity=4
    assert ("entry", "BUY", 4) in ex.calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_contracts_step.py -q`
Expected: FAIL — `manage_step()` has no `contracts` kwarg (TypeError) / entry order has no quantity.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/live.py`, edit `manage_step`:

Change the signature to add `contracts`:
```python
async def manage_step(day: date, source, params, executor, position: Position | None,
                      events=None, vix=None, contracts: int = 1) -> dict:
```

In the **entry** branch (`if position is None:`), after `pos = decide_entry(...)` and the `None` guard, set contracts and pass quantity:
```python
        pos.contracts = contracts
        await executor(pos.underlying, entry_legs(pos), net_price=round(pos.net_debit, 2),
                       action="BUY", reason="entry", quantity=contracts)
```

In the **adjust** branch, add `quantity=position.contracts`:
```python
        await executor(position.underlying, adjustment_legs(cal), net_price=None,
                       action="BUY", reason="adjust", quantity=position.contracts)
```

In the **exit** branch, add `quantity=position.contracts` to the SELL:
```python
        await executor(position.underlying, exit_legs(position), net_price=None,
                       action="SELL", reason=info.get("reason", "exit"), quantity=position.contracts)
```

In the **roll** branch (the `if act == "roll":` block): the close SELL uses the old position's contracts, and the re-entry stamps + orders the new contracts:
```python
        await executor(position.underlying, exit_legs(position), net_price=None,
                       action="SELL", reason=f"roll_{reason}", quantity=position.contracts)
        new_pos = decide_entry(day, source, params, events, vix) if vix is not None else None
        if new_pos is not None:
            new_pos.contracts = contracts
            await executor(new_pos.underlying, entry_legs(new_pos),
                           net_price=round(new_pos.net_debit, 2), action="BUY",
                           reason="roll_entry", quantity=contracts)
```
(Leave the rest of the roll branch — the returned dicts — unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tc_contracts_step.py -q && ../.venv/bin/python -m pytest tests/test_tc_manage_step.py tests/test_tc_roll.py -q`
Expected: PASS (new test + existing manage_step/roll tests still green — their `Exec` spies accept `**kw`, so the extra `quantity` kwarg is harmless).

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/live.py backend/tests/test_tc_contracts_step.py
git commit -m "feat(contracts): manage_step stamps position.contracts and orders that quantity"
```

---

### Task 4: run_live reads the setting; store persists/restores contracts

**Files:**
- Modify: `backend/app/strategies/triple_calendar/store.py` (`save_position`, `load_open`)
- Modify: `backend/app/strategies/triple_calendar/strategy_class.py` (`run_live` + a `_contracts` helper)
- Test: `backend/tests/test_tc_contracts_live.py`

**Interfaces:**
- Consumes: `manage_step(..., contracts=...)` (Task 3), `StrategyState.contracts` (Task 1), `Position.contracts`.
- Produces: `save_position` stores `pos.contracts`; `load_open` restores it; `run_live` passes `contracts=StrategyState.contracts` (min 1) into `manage_step`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_contracts_live.py
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
        # user set 3 contracts on the strategy
        async with session_scope() as s:
            st = await s.get(StrategyState, "strategy_2")
            if st is None:
                st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
                s.add(st)
            st.contracts = 3

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
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        async with session_scope() as s:
            rec = (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == "strategy_2",
                OptionPositionRecord.status == "open"))).scalars().first()
        assert rec is not None and rec.contracts == 3, f"expected contracts=3, got {rec and rec.contracts}"
        print(f"PASS: entered position persisted contracts={rec.contracts}")

    asyncio.run(_t())
    print("\nALL TC-CONTRACTS-LIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_cl.db && DATABASE_URL=sqlite+aiosqlite:///./_t_cl.db ../.venv/bin/python -m tests.test_tc_contracts_live 2>&1 | tail -4; rm -f _t_cl.db`
Expected: FAIL — the persisted record's `contracts` is 1 (loop doesn't read the setting yet).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/strategies/triple_calendar/store.py`:
- In `save_position`, add `contracts=pos.contracts` to the `OptionPositionRecord(...)` constructor.
- In `load_open`, after building the deserialized `Position`, set its contracts from the record. Change the deserialize/return so the restored `Position` gets `pos.contracts = rec.contracts` before returning (e.g. build `pos = _deser(...)`, then `pos.contracts = rec.contracts or 1`, then `return pos`).

In `backend/app/strategies/triple_calendar/strategy_class.py`:
- Add a helper method on the strategy class:
```python
    async def _contracts(self) -> int:
        from app.db.base import session_scope
        from app.db.models import StrategyState
        async with session_scope() as s:
            st = await s.get(StrategyState, self.key)
        return max(1, int(getattr(st, "contracts", 1) or 1))
```
- In `run_live`, where it calls `manage_step(today, snapshot, params, executor, position, vix=vix)`, change it to read and pass contracts:
```python
                        contracts = await self._contracts()
                        out = await manage_step(today, snapshot, params, executor, position,
                                                vix=vix, contracts=contracts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_cl.db && DATABASE_URL=sqlite+aiosqlite:///./_t_cl.db ../.venv/bin/python -m tests.test_tc_contracts_live 2>&1 | tail -3; rm -f _t_cl.db`
Expected: `ALL TC-CONTRACTS-LIVE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/store.py backend/app/strategies/triple_calendar/strategy_class.py backend/tests/test_tc_contracts_live.py
git commit -m "feat(contracts): live loop reads StrategyState.contracts; store persists/restores it"
```

---

### Task 5: strategies router — `/contracts` endpoint + detail + P&L ×contracts

**Files:**
- Modify: `backend/app/routers/strategies.py` (add route; add `contracts` to `detail` + `options_position`)
- Test: `backend/tests/test_contracts_api.py`

**Interfaces:**
- Consumes: `StrategyState.contracts`, `OptionPositionRecord.contracts`.
- Produces: `PUT /api/strategies/{key}/contracts` `{contracts:int}` (clamp ≥1) → `{key, contracts}`; `detail` response includes `contracts`; `options_position` returns `contracts` and scales `unrealized_pnl` by it.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_contracts_api.py
from datetime import date, timedelta

import pytest

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord
from app.routers import strategies


@pytest.mark.asyncio
async def test_set_contracts_clamps_and_persists():
    await init_db()
    out = await strategies.set_contracts("strategy_2", strategies.ContractsBody(contracts=0))
    assert out["contracts"] == 1        # clamped to >= 1
    out = await strategies.set_contracts("strategy_2", strategies.ContractsBody(contracts=3))
    assert out["contracts"] == 3
    detail = await strategies.detail("strategy_2")
    assert detail["contracts"] == 3


@pytest.mark.asyncio
async def test_options_position_scales_pnl_by_contracts():
    await init_db()
    today = date.today()
    async with session_scope() as s:
        rec = OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="open", paper=True,
            net_debit=4.0, contracts=2, last_value=4.2, last_pnl_pct=0.05, last_spot=501.2,
            calendars=[{"strike": 500.0, "sell": (today + timedelta(days=21)).isoformat(),
                        "buy": (today + timedelta(days=28)).isoformat()}], order_ids=[])
        s.add(rec)
    out = await strategies.options_position("strategy_2")
    assert out["contracts"] == 2
    # (4.2 - 4.0) * 100 * 2 = 40.0
    assert out["unrealized_pnl"] == pytest.approx(40.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_ca.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ca.db ../.venv/bin/python -m pytest tests/test_contracts_api.py -q; rm -f _t_ca.db`
Expected: FAIL — `strategies.set_contracts`/`ContractsBody` don't exist; `contracts` not in responses; pnl not ×2.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/routers/strategies.py`:

Add near the `AllocationBody` class:
```python
class ContractsBody(BaseModel):
    contracts: int = Field(1, ge=1)
```
(Confirm `Field` is imported from pydantic at the top; the file already imports `BaseModel, Field`.)

Add a route (next to `set_allocation`), auth-gated like allocation:
```python
@router.put("/{key}/contracts", dependencies=[Depends(require_auth)])
async def set_contracts(key: str, body: ContractsBody) -> dict:
    n = max(1, int(body.contracts))
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st is None:
            raise HTTPException(status_code=404, detail="Unknown strategy")
        st.contracts = n
    await log_event(f"Contracts set to {n}", category="action", strategy_key=key)
    return {"key": key, "contracts": n}
```

In the `detail` function's returned dict, add:
```python
        "contracts": st.contracts if st else 1,
```

In `options_position`: read `rec.contracts` and use it. Where the payload computes
`unrealized_pnl`, change it to scale by contracts, and add `contracts` to the payload. Concretely,
after loading `rec`, add `contracts = rec.contracts or 1`, include `"contracts": contracts` in the
`out` dict, and change the unrealized-pnl line to:
```python
        "unrealized_pnl": round((value - net) * 100 * contracts, 2) if value is not None else None,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_ca.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ca.db ../.venv/bin/python -m pytest tests/test_contracts_api.py -q; rm -f _t_ca.db`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/strategies.py backend/tests/test_contracts_api.py
git commit -m "feat(contracts): PUT /contracts endpoint + detail.contracts + options-position P&L x contracts"
```

---

### Task 6: Triple Calendar Controls — Contracts field

**Files:**
- Modify: `frontend/src/api.js` (add `setContracts`)
- Modify: `frontend/src/components/OptionsStrategyDetail.jsx` (Controls card)

**Interfaces:**
- Consumes: `PUT /api/strategies/{key}/contracts` (Task 5).

- [ ] **Step 1: Add the API method**

In `frontend/src/api.js`, after the `allocate:` method, add:
```javascript
  setContracts: (key, contracts) =>
    request(`/api/strategies/${key}/contracts`, {
      method: "PUT",
      body: JSON.stringify({ contracts }),
    }),
```

- [ ] **Step 2: Add the Contracts input to Controls**

In `frontend/src/components/OptionsStrategyDetail.jsx`, add a local state near the other `useState` hooks (after `const [alloc, setAlloc] = useState("");`):
```javascript
  const [contracts, setContracts] = useState("");
```
Then in the Controls card, right after the "Set Allocation" `<button>` … `</button>` line and its allocation input row, add a contracts row (place it just before the `Current allocation` muted line):
```javascript
        <div className="row gap center" style={{ marginTop: 8 }}>
          <input
            className="input tiny"
            type="number"
            min="1"
            placeholder="Contracts"
            value={contracts}
            onChange={(e) => setContracts(e.target.value)}
          />
          <button className="btn" onClick={() => act(() => api.setContracts(keyName, Math.max(1, Number(contracts) || 1)))}>Set Contracts</button>
          <span className="muted small">Current: {detail.contracts ?? 1} contract{(detail.contracts ?? 1) === 1 ? "" : "s"}</span>
        </div>
```
(`detail.contracts` comes from the `/{key}` detail response — Task 5.)

- [ ] **Step 3: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.js frontend/src/components/OptionsStrategyDetail.jsx
git commit -m "feat(contracts): TC Controls contracts field + setContracts api"
```

---

### Task 7: Builder — contracts column, SaveBody, store

**Files:**
- Modify: `backend/app/db/models.py` (`BuilderPositionRecord`)
- Modify: `backend/app/db/base.py` (`_ensure_added_columns` — `builder_positions`)
- Modify: `backend/app/options/store.py` (`save_trade`)
- Modify: `backend/app/routers/builder.py` (`SaveBody`, `save_trade` route)
- Test: `backend/tests/test_builder_contracts.py`

**Interfaces:**
- Produces: `BuilderPositionRecord.contracts: int` (default 1); `store.save_trade(..., contracts: int = 1)`; `SaveBody.contracts: int` (ge 1).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_contracts.py
import pytest

from app.db.base import init_db
from app.options import store

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0, "expiry": "2026-07-31", "qty": 1}]


@pytest.mark.asyncio
async def test_save_trade_stores_contracts():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25, contracts=3)
    assert (await store.get_trade(tid)).contracts == 3
    tid2 = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                  target_pct=None, stop_pct=None)
    assert (await store.get_trade(tid2)).contracts == 1  # default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_bc.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bc.db ../.venv/bin/python -m pytest tests/test_builder_contracts.py -q; rm -f _t_bc.db`
Expected: FAIL — `save_trade()` has no `contracts` kwarg / no column.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/db/models.py`, in `BuilderPositionRecord`, add after `iv`:
```python
    contracts: Mapped[int] = mapped_column(Integer, default=1)
```

In `backend/app/db/base.py`, in `_ensure_added_columns`, add a block (after the `option_positions` block):
```python
    if "builder_positions" in tables:
        cols = {c["name"] for c in insp.get_columns("builder_positions")}
        if "contracts" not in cols:
            conn.execute(text("ALTER TABLE builder_positions ADD COLUMN contracts INTEGER DEFAULT 1"))
```

In `backend/app/options/store.py`, change `save_trade` to accept and store contracts:
```python
async def save_trade(underlying: str, legs: list[dict], net_debit: float, iv: float,
                     target_pct: float | None, stop_pct: float | None,
                     contracts: int = 1) -> int:
```
and add `contracts=max(1, int(contracts))` to the `BuilderPositionRecord(...)` constructor.

In `backend/app/routers/builder.py`, add to `SaveBody`:
```python
    contracts: int = Field(1, ge=1)
```
and in the `save_trade` route, pass it through:
```python
    tid = await store.save_trade(body.underlying, _legs(body.legs), body.net_debit,
                                 body.iv, body.target_pct, body.stop_pct, body.contracts)
```
Also add `"contracts": rec.contracts` to `_serialize(rec)` so the list/detail responses expose it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_bc.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bc.db ../.venv/bin/python -m pytest tests/test_builder_contracts.py tests/test_builder_api.py -q; rm -f _t_bc.db`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/db/base.py backend/app/options/store.py backend/app/routers/builder.py backend/tests/test_builder_contracts.py
git commit -m "feat(contracts): builder_positions.contracts + SaveBody/save_trade contracts"
```

---

### Task 8: Builder monitor — P&L ×contracts

**Files:**
- Modify: `backend/app/options/monitor.py` (`value_open_trades_once`)
- Test: `backend/tests/test_builder_monitor.py` (extend)

**Interfaces:**
- Consumes: `BuilderPositionRecord.contracts` (Task 7).
- Produces: monitor `last_pnl = round((value − net_debit) × 100 × contracts, 2)`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_builder_monitor.py`:

```python
@pytest.mark.asyncio
async def test_pnl_scales_by_contracts():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=None, stop_pct=None, contracts=3)
    await monitor.value_open_trades_once(_mids(2.1))   # +0.10/1x
    t = await store.get_trade(tid)
    assert t.last_pnl == pytest.approx(30.0)           # (2.1-2.0)*100*3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_mon.db && DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db ../.venv/bin/python -m pytest tests/test_builder_monitor.py::test_pnl_scales_by_contracts -q; rm -f _t_mon.db`
Expected: FAIL — `last_pnl` is 10.0 (not scaled).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/options/monitor.py`, in `value_open_trades_once`, change the pnl line from:
```python
            pnl = round((value - t.net_debit) * 100, 2)
```
to:
```python
            pnl = round((value - t.net_debit) * 100 * (t.contracts or 1), 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_mon.db && DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db ../.venv/bin/python -m pytest tests/test_builder_monitor.py -q; rm -f _t_mon.db`
Expected: PASS (all monitor tests, including the new one).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/monitor.py backend/tests/test_builder_monitor.py
git commit -m "feat(contracts): builder monitor scales tracked P&L by contracts"
```

---

### Task 9: Builder save panel — Contracts field

**Files:**
- Modify: `frontend/src/components/BuilderTab.jsx` (Save panel + save call)

**Interfaces:**
- Consumes: `POST /api/builder/trades` with `contracts` (Task 7).

- [ ] **Step 1: Add the Contracts input + include it in the save**

In `frontend/src/components/BuilderTab.jsx`:
- Add state near the other `useState` hooks (after `const [stop, setStop] = useState(25);`):
```javascript
  const [contracts, setContracts] = useState(1);
```
- In `save`, include contracts in the body — change the `api.builderSaveTrade({...})` object to add:
```javascript
        contracts: Math.max(1, Number(contracts) || 1),
```
- In the "Save as tracked trade" controls row, add a Contracts input before the "Save trade" button:
```javascript
          <label className="muted small">Contracts</label>
          <input className="input tiny" type="number" min="1" value={contracts} onChange={(e) => setContracts(e.target.value)} />
```

- [ ] **Step 2: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/BuilderTab.jsx
git commit -m "feat(contracts): builder save-panel contracts multiplier"
```

---

### Task 10: Full check + live verification

**Files:** none (verification only).

- [ ] **Step 1: Backend suite + import check**

Run:
```bash
cd backend
../.venv/bin/python -c "import app.main; print('main imports OK')"
rm -f _t_all.db
DATABASE_URL=sqlite+aiosqlite:///./_t_all.db ../.venv/bin/python -m pytest \
  tests/test_contracts_columns.py tests/test_combo_quantity.py tests/test_tc_contracts_step.py \
  tests/test_contracts_api.py tests/test_builder_contracts.py tests/test_builder_monitor.py \
  tests/test_tc_manage_step.py tests/test_tc_roll.py tests/test_builder_api.py -q
rm -f _t_all.db
for t in test_tc_contracts_live test_tc_roll_live test_tc_live_persist test_tc_vix_live; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -E "PASSED|assert|Error" | tail -1
  rm -f _t_$t.db
done
```
Expected: `main imports OK`; all pytest pass; each standalone test prints its `... PASSED`.

- [ ] **Step 2: Rebuild frontend + restart + smoke the endpoints**

Run:
```bash
cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep "built in"
cd .. && launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
for i in $(seq 1 40); do curl -s -m3 http://127.0.0.1:8765/health 2>/dev/null | grep -q '"status":"ok"' && break; sleep 1; done
curl -s -m5 -X PUT http://127.0.0.1:8765/api/strategies/strategy_2/contracts -H "Content-Type: application/json" -d '{"contracts":2}'
echo ""
curl -s -m5 http://127.0.0.1:8765/api/strategies/strategy_2 | python3 -c "import sys,json;print('detail.contracts =', json.load(sys.stdin)['contracts'])"
```
Expected: `{"key":"strategy_2","contracts":2}` then `detail.contracts = 2`.

- [ ] **Step 3: Reset the demo setting + manual check**

Reset TC contracts back to 1 so the running strategy isn't left at 2 from the smoke test:
```bash
curl -s -m5 -X PUT http://127.0.0.1:8765/api/strategies/strategy_2/contracts -H "Content-Type: application/json" -d '{"contracts":1}' && echo " (reset to 1)"
```
Then in the app: Strategies → Triple Calendar → Controls shows a **Contracts** field ("Current: 1 contract"); Builder → Save panel shows a **Contracts** input. (Live P&L scaling shows once positions exist / market open.)

- [ ] **Step 4: Final commit (only if manual tweaks were needed)**

```bash
git add -A && git commit -m "test(contracts): verify end-to-end" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** StrategyState/OptionPositionRecord/Position contracts + migrations (T1); submit_combo_order quantity (T2); manage_step threading incl. roll (T3); run_live reads setting + store persist/restore (T4); /contracts endpoint + detail + options-position P&L×contracts (T5); TC Controls UI (T6); builder_positions column + SaveBody/store (T7); monitor P&L×contracts (T8); builder save UI (T9); regression + live (T10). All covered.
- **Semantics:** contracts read fresh each tick → next entry; open position keeps its stored contracts (T3/T4); rolls re-enter at current setting (T3).
- **Type consistency:** `contracts: int` everywhere; `Position.contracts`; `submit_combo_order(..., quantity=)`; `manage_step(..., contracts=)`; `save_trade(..., contracts=)`; `ContractsBody.contracts`; `SaveBody.contracts`. P&L `×100×contracts` in both the options-position endpoint (T5) and the monitor (T8).
- **Placeholder scan:** none.
