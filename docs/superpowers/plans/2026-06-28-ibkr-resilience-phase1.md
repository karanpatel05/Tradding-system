# IBKR Resilience — Phase 1 (Safety-Critical Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the live engine never trade on a wrong picture — detect IBKR disconnects, auto-reconnect, reconcile against broker truth on every (re)connect, never double-submit an order, persist/rebuild worker state across restarts, and HALT into safe mode when anything cannot be reconciled.

**Architecture:** A single connection-state machine (upgraded `Gateway`) owns the IB lifecycle and publishes state to subscribers. A `RecoveryCoordinator` runs one deterministic sequence on every transition into CONNECTED: reconcile (orders→fills→positions) → rebuild durable worker state → cross-check → READY or SAFE_MODE. Workers pause unless state is READY. Orders carry a durable `client_order_ref` so a reconnect dedupes instead of resubmitting.

**Tech Stack:** Python 3.14, asyncio, `ib_async` 2.1.0, async SQLAlchemy (SQLite dev / Postgres prod), existing engine manager + journal.

**Source spec:** `docs/superpowers/specs/2026-06-28-ibkr-resilience-design.md`

## Global Constraints

- Library: `ib_async` 2.1.0; one IB client per `clientId` (a duplicate clientId hangs).
- Trading is permitted ONLY when connection state is `READY`. `SAFE_MODE` and `ENABLE_LIVE_ORDERS=false` each independently block all order transmission.
- Keep `app.ibkr.gateway.get_gateway()` returning the existing singleton; upgrade the class in place — do not rename or break its public methods (`connect`, `get_ib`, `is_connected`, `disconnect`).
- Reconciliation scopes to orders THIS system placed (matched by `client_order_ref`/`permId`); any other account activity is "unknown" → SAFE_MODE (never adopt it).
- Tests are offline against a stubbed IB (no live Gateway). Test style: a module-level `async def _run()` with `assert` + `print("PASS: …")`, ending `print("\nALL … TESTS PASSED")`, run via `DATABASE_URL=sqlite+aiosqlite:///./_t.db python -m tests.<name>`. DB-touching tests set `DATABASE_URL` to a throwaway sqlite file and call `await init_db()`.
- Exact ib_async method/event names are verified against the installed package during Task 0.

---

### Task 0: Verify ib_async surface (spike, no production code)

> **Pre-validated 2026-06-28 against ib_async 2.1.0:** all of `disconnectedEvent`,
> `connectedEvent`, `errorEvent`, `reqCurrentTimeAsync`, `reqAllOpenOrdersAsync`,
> `reqExecutionsAsync`, `reqPositionsAsync`, `positions`, `ExecutionFilter.time`, and
> `Order.orderRef` are present. You may skip the spike unless the installed version changed;
> the steps below remain for that case.

**Files:**
- Create: `backend/scripts/ib_surface_check.py` (throwaway spike; deleted at end of task)

**Interfaces:**
- Produces: confirmed names used by every later task — `IB.disconnectedEvent`, `IB.connectedEvent`, `IB.errorEvent`, `IB.reqCurrentTimeAsync`, `IB.reqAllOpenOrdersAsync`, `IB.reqExecutionsAsync`, `ExecutionFilter`, `IB.reqPositionsAsync`, `IB.positions`, `Order.orderRef`, `Trade.order.permId`.

- [ ] **Step 1: Write the spike**

```python
# backend/scripts/ib_surface_check.py
"""Spike: confirm the ib_async attribute/method names this feature depends on.
Does NOT connect; only inspects the classes. Delete after Task 0."""
from ib_async import IB, ExecutionFilter, Order  # noqa: F401

ib = IB()
needed_events = ["disconnectedEvent", "connectedEvent", "errorEvent"]
needed_methods = ["reqCurrentTimeAsync", "reqAllOpenOrdersAsync",
                  "reqExecutionsAsync", "reqPositionsAsync", "positions"]
for n in needed_events + needed_methods:
    assert hasattr(ib, n), f"MISSING IB.{n}"
assert "time" in ExecutionFilter().__dict__ or hasattr(ExecutionFilter(), "time"), "ExecutionFilter.time?"
assert hasattr(Order(), "orderRef"), "MISSING Order.orderRef"
print("OK: all ib_async names present")
```

- [ ] **Step 2: Run it**

Run: `cd backend && ../.venv/bin/python -m scripts.ib_surface_check`
Expected: `OK: all ib_async names present`. If any line reports MISSING, note the real name and substitute it everywhere in this plan before continuing.

- [ ] **Step 3: Delete the spike and commit the note**

```bash
cd backend && rm scripts/ib_surface_check.py
git add -A && git commit -m "chore(ibkr): verify ib_async 2.1.0 surface for resilience work"
```

---

### Task 1: Connection-state machine + subscribers on `Gateway`

**Files:**
- Modify: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_conn_state.py`

**Interfaces:**
- Produces: `ConnState` (enum: `DISCONNECTED, CONNECTING, CONNECTED, RECOVERING, READY, SAFE_MODE`); `Gateway.state -> ConnState`; `Gateway.set_state(s: ConnState) -> None`; `Gateway.subscribe(cb: Callable[[ConnState], None]) -> None`. `set_state` notifies every subscriber synchronously and is a no-op if state is unchanged.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_conn_state.py
"""Connection-state machine notifies subscribers on change only.
   Run: python -m tests.test_conn_state
"""
from __future__ import annotations


def _run() -> None:
    from app.ibkr.gateway import ConnState, Gateway

    gw = Gateway()
    seen: list[ConnState] = []
    gw.subscribe(seen.append)

    assert gw.state is ConnState.DISCONNECTED, gw.state
    gw.set_state(ConnState.CONNECTING)
    gw.set_state(ConnState.CONNECTING)          # duplicate → no notify
    gw.set_state(ConnState.CONNECTED)
    assert seen == [ConnState.CONNECTING, ConnState.CONNECTED], seen
    print("PASS: state machine notifies on change only")

    print("\nALL CONN-STATE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_conn_state`
Expected: FAIL — `ImportError: cannot import name 'ConnState'`.

- [ ] **Step 3: Implement on `Gateway`**

Add to `backend/app/ibkr/gateway.py` (imports at top, enum above the class, fields in `__init__`, methods on the class):

```python
import enum
from typing import Callable


class ConnState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECOVERING = "recovering"
    READY = "ready"
    SAFE_MODE = "safe_mode"
```

In `Gateway.__init__`, after `self._ib = None`:

```python
        self._state = ConnState.DISCONNECTED
        self._subscribers: list[Callable[[ConnState], None]] = []
```

Add methods:

```python
    @property
    def state(self) -> ConnState:
        return self._state

    def subscribe(self, cb: "Callable[[ConnState], None]") -> None:
        self._subscribers.append(cb)

    def set_state(self, s: ConnState) -> None:
        if s is self._state:
            return
        self._state = s
        logger.warning("Connection state -> %s", s.value)
        for cb in list(self._subscribers):
            try:
                cb(s)
            except Exception:  # noqa: BLE001 — a bad subscriber must not break transitions
                logger.exception("conn-state subscriber failed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_conn_state`
Expected: `ALL CONN-STATE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/tests/test_conn_state.py
git commit -m "feat(ibkr): connection-state machine + subscribers on Gateway"
```

---

### Task 2: Disconnect/connect/error event wiring

**Files:**
- Modify: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_conn_events.py`

**Interfaces:**
- Consumes: `ConnState`, `Gateway.set_state`.
- Produces: `Gateway._wire_events(ib)` registers handlers on `ib.disconnectedEvent`/`ib.errorEvent`; `Gateway.data_lost: bool` (True after IBKR error 1101, cleared on next successful recovery). Handler maps: disconnect → `DISCONNECTED`; error `1100` → `DISCONNECTED`; `1101` → set `data_lost=True`; `1102` → leave `data_lost` False. `connect()` calls `_wire_events` after connecting.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_conn_events.py
"""ib_async events drive connection state; 1101 flags data loss.
   Run: python -m tests.test_conn_events
"""
from __future__ import annotations


def _run() -> None:
    from app.ibkr.gateway import ConnState, Gateway

    class FakeEvent:
        def __init__(self): self._h = []
        def __iadd__(self, h): self._h.append(h); return self
        def emit(self, *a): [h(*a) for h in self._h]

    class FakeIB:
        def __init__(self):
            self.disconnectedEvent = FakeEvent()
            self.connectedEvent = FakeEvent()
            self.errorEvent = FakeEvent()

    gw = Gateway()
    ib = FakeIB()
    gw._wire_events(ib)
    gw.set_state(ConnState.READY)

    ib.disconnectedEvent.emit()
    assert gw.state is ConnState.DISCONNECTED, gw.state
    print("PASS: disconnect event -> DISCONNECTED")

    # errorEvent signature: (reqId, errorCode, errorString, contract)
    ib.errorEvent.emit(-1, 1101, "connectivity restored - data lost", None)
    assert gw.data_lost is True
    ib.errorEvent.emit(-1, 1102, "connectivity restored - data maintained", None)
    # 1102 does not clear a prior data_lost within the same outage; recovery clears it.
    print("PASS: 1101 sets data_lost")

    print("\nALL CONN-EVENT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_conn_events`
Expected: FAIL — `AttributeError: 'Gateway' object has no attribute '_wire_events'`.

- [ ] **Step 3: Implement**

In `Gateway.__init__`, after the subscriber list: `self.data_lost = False`.

Add method and call it from `connect()` (right after `self._ib = ib`):

```python
    def _wire_events(self, ib) -> None:
        ib.disconnectedEvent += self._on_disconnect
        ib.errorEvent += self._on_error

    def _on_disconnect(self, *_) -> None:
        self.set_state(ConnState.DISCONNECTED)

    def _on_error(self, reqId, code, msg, *_) -> None:
        if code == 1100:
            self.set_state(ConnState.DISCONNECTED)
        elif code == 1101:
            self.data_lost = True
        # 1102 = restored, data maintained → nothing to flag here.
```

In `connect()`, after `self._ib = ib` and before the log line, add: `self._wire_events(ib)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_conn_events`
Expected: `ALL CONN-EVENT TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/tests/test_conn_events.py
git commit -m "feat(ibkr): wire disconnect/error events (1100/1101/1102) to state"
```

---

### Task 3: Auto-reconnect loop with exponential backoff

**Files:**
- Modify: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_reconnect_backoff.py`

**Interfaces:**
- Consumes: `ConnState`, `Gateway.set_state`, `Gateway.connect`.
- Produces: `Gateway.backoff_schedule(attempt: int) -> float` (pure: `min(60.0, 2 ** attempt)` seconds, attempt ≥ 0); `async Gateway.reconnect_loop(max_attempts: int | None = None)` — loops calling `connect()`, sleeping `backoff_schedule(attempt)` between failures, until connected or `max_attempts` reached. Jitter is applied in the loop but `backoff_schedule` itself is deterministic for testing.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_reconnect_backoff.py
"""Backoff schedule is bounded/exponential; reconnect loop retries then connects.
   Run: python -m tests.test_reconnect_backoff
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.ibkr.gateway import Gateway

    gw = Gateway()
    assert gw.backoff_schedule(0) == 1.0, gw.backoff_schedule(0)
    assert gw.backoff_schedule(3) == 8.0
    assert gw.backoff_schedule(10) == 60.0          # capped
    print("PASS: backoff schedule bounded + exponential")

    async def _loop_test():
        calls = {"n": 0}
        async def fake_connect():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("nope")
            return object()
        gw.connect = fake_connect          # type: ignore[assignment]
        gw.backoff_schedule = lambda a: 0.0  # type: ignore[assignment]  # no real waiting
        await gw.reconnect_loop(max_attempts=5)
        assert calls["n"] == 3, calls
    asyncio.run(_loop_test())
    print("PASS: reconnect loop retries until connected")

    print("\nALL RECONNECT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_reconnect_backoff`
Expected: FAIL — `AttributeError: 'Gateway' object has no attribute 'backoff_schedule'`.

- [ ] **Step 3: Implement**

Add to `gateway.py` (module imports: `import asyncio`, `import random`) and to the class:

```python
    @staticmethod
    def backoff_schedule(attempt: int) -> float:
        return min(60.0, float(2 ** attempt))

    async def reconnect_loop(self, max_attempts: int | None = None) -> None:
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            try:
                self.set_state(ConnState.CONNECTING)
                await self.connect()
                return
            except Exception:  # noqa: BLE001 — keep retrying through any connect failure
                delay = self.backoff_schedule(attempt) + random.uniform(0, 0.5)
                logger.warning("reconnect attempt %d failed; retrying in %.1fs", attempt, delay)
                await asyncio.sleep(delay)
                attempt += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_reconnect_backoff`
Expected: `ALL RECONNECT TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/tests/test_reconnect_backoff.py
git commit -m "feat(ibkr): exponential-backoff reconnect loop"
```

---

### Task 4: Heartbeat watchdog

**Files:**
- Modify: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_heartbeat.py`

**Interfaces:**
- Consumes: `Gateway.is_connected`, `Gateway.set_state`, `ConnState`.
- Produces: `async Gateway.heartbeat_once(timeout: float = 5.0) -> bool` — calls `ib.reqCurrentTimeAsync()` under `asyncio.wait_for`; returns True on success, and on timeout/error sets state `DISCONNECTED` and returns False.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_heartbeat.py
"""Heartbeat miss flips state to DISCONNECTED.
   Run: python -m tests.test_heartbeat
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.ibkr.gateway import ConnState, Gateway

    class HangingIB:
        def isConnected(self): return True
        async def reqCurrentTimeAsync(self):
            await asyncio.sleep(10)   # never returns within timeout

    async def _t():
        gw = Gateway()
        gw._ib = HangingIB()
        gw.set_state(ConnState.READY)
        ok = await gw.heartbeat_once(timeout=0.05)
        assert ok is False
        assert gw.state is ConnState.DISCONNECTED, gw.state
    asyncio.run(_t())
    print("PASS: heartbeat timeout -> DISCONNECTED")

    print("\nALL HEARTBEAT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_heartbeat`
Expected: FAIL — `AttributeError: ... 'heartbeat_once'`.

- [ ] **Step 3: Implement**

```python
    async def heartbeat_once(self, timeout: float = 5.0) -> bool:
        if self._ib is None:
            return False
        try:
            await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 — timeout or socket error both mean "dead"
            logger.warning("heartbeat missed; marking DISCONNECTED")
            self.set_state(ConnState.DISCONNECTED)
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_heartbeat`
Expected: `ALL HEARTBEAT TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/tests/test_heartbeat.py
git commit -m "feat(ibkr): heartbeat watchdog via reqCurrentTime"
```

---

### Task 5: Persisted SAFE_MODE flag + order gate

**Files:**
- Modify: `backend/app/db/models.py` (add `SystemFlag`)
- Create: `backend/app/engine/safe_mode.py`
- Modify: `backend/app/engine/execution.py` (gate `submit_equity_order` + `submit_bracket_order`)
- Test: `backend/tests/test_safe_mode.py`

**Interfaces:**
- Produces: model `SystemFlag(key: str pk, on: bool, reason: str, updated_at)`; `async trip_safe_mode(reason: str) -> None`; `async clear_safe_mode() -> None`; `async is_safe_mode() -> bool` (reads the `"safe_mode"` row, default False). Execution submitters return `{"transmitted": False, "safe_mode": True, "detail": ...}` without touching IB when safe mode is on.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_safe_mode.py
"""Safe mode persists and blocks order submission.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_safe.db python -m tests.test_safe_mode
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.safe_mode import clear_safe_mode, is_safe_mode, trip_safe_mode
        from app.engine.execution import submit_equity_order

        await init_db()
        assert await is_safe_mode() is False
        await trip_safe_mode("orphan position AAPL")
        assert await is_safe_mode() is True

        res = await submit_equity_order("AAPL", "enter_long", 10, reason="entry")
        assert res["transmitted"] is False and res.get("safe_mode") is True, res
        print("PASS: safe mode blocks submission")

        await clear_safe_mode()
        assert await is_safe_mode() is False
        print("PASS: safe mode clears")

    asyncio.run(_t())
    print("\nALL SAFE-MODE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_safe.db ../.venv/bin/python -m tests.test_safe_mode`
Expected: FAIL — `ModuleNotFoundError: app.engine.safe_mode`.

- [ ] **Step 3: Implement model + module + gate**

Add to `models.py` after `JournalNote`:

```python
class SystemFlag(Base):
    """Tiny key/value flags for engine-wide state (e.g. safe_mode)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    on: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
```

Create `app/engine/safe_mode.py`:

```python
"""Engine-wide SAFE_MODE flag (persisted). When on, ALL order submission is blocked
until a human clears it. Independent of ENABLE_LIVE_ORDERS."""
from __future__ import annotations

import logging

from app.db.base import session_scope
from app.db.models import SystemFlag

logger = logging.getLogger("trading.safe_mode")
_KEY = "safe_mode"


async def trip_safe_mode(reason: str) -> None:
    async with session_scope() as s:
        row = await s.get(SystemFlag, _KEY)
        if row is None:
            s.add(SystemFlag(key=_KEY, on=True, reason=reason))
        else:
            row.on, row.reason = True, reason
    logger.error("SAFE MODE TRIPPED: %s", reason)


async def clear_safe_mode() -> None:
    async with session_scope() as s:
        row = await s.get(SystemFlag, _KEY)
        if row is not None:
            row.on, row.reason = False, ""
    logger.warning("safe mode cleared")


async def is_safe_mode() -> bool:
    async with session_scope() as s:
        row = await s.get(SystemFlag, _KEY)
        return bool(row and row.on)
```

In `execution.py`, at the very top of both `submit_equity_order` and `submit_bracket_order` (before the quantity/enable_live checks), add:

```python
    from app.engine.safe_mode import is_safe_mode
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({symbol})"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_safe.db ../.venv/bin/python -m tests.test_safe_mode && rm -f _t_safe.db`
Expected: `ALL SAFE-MODE TESTS PASSED`.

**Regression (shared file `execution.py`):** also run the existing execution test —
`cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_execution_ibkr && rm -f _t.db` — and confirm it still passes (the safe-mode gate must not break paper/live submission when safe mode is off).

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/engine/safe_mode.py backend/app/engine/execution.py backend/tests/test_safe_mode.py
git commit -m "feat(engine): persisted SAFE_MODE flag gating all order submission"
```

---

### Task 6: Durable order intents (idempotency)

**Files:**
- Modify: `backend/app/db/models.py` (add `OrderIntent`)
- Create: `backend/app/engine/order_store.py`
- Test: `backend/tests/test_order_store.py`

**Interfaces:**
- Produces: model `OrderIntent(id, client_order_ref: str unique, strategy_key, symbol, intent, quantity: int, perm_id: str | None, status: str, created_at)`; `new_ref(strategy_key, symbol) -> str` (pure: `f"{strategy_key}-{symbol}-{uuid4().hex[:8]}"`); `async record_intent(strategy_key, symbol, intent, quantity) -> str` (writes a `submitting` row, returns the ref); `async mark_perm_id(ref, perm_id) -> None`; `async set_status(ref, status) -> None`; `async open_refs() -> list[str]` (refs whose status is `submitting` or `working`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_order_store.py
"""Order intents are durable and dedupable by ref.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_ord.db python -m tests.test_order_store
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.order_store import (mark_perm_id, new_ref, open_refs,
                                            record_intent, set_status)

        await init_db()
        ref = new_ref("strategy_1", "AAPL")
        assert ref.startswith("strategy_1-AAPL-") and len(ref) > len("strategy_1-AAPL-")
        r2 = await record_intent("strategy_1", "AAPL", "enter_long", 10)
        assert (await open_refs()) == [r2], await open_refs()
        await mark_perm_id(r2, "987654")
        await set_status(r2, "filled")
        assert (await open_refs()) == [], "filled intent should not be open"
        print("PASS: intent lifecycle submitting->filled")

    asyncio.run(_t())
    print("\nALL ORDER-STORE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_ord.db ../.venv/bin/python -m tests.test_order_store`
Expected: FAIL — `ModuleNotFoundError: app.engine.order_store`.

- [ ] **Step 3: Implement model + store**

Add to `models.py` after `SystemFlag`:

```python
class OrderIntent(Base):
    """Durable record of an order we intend to / did transmit, written BEFORE
    transmit so a reconnect dedupes by ref/permId instead of resubmitting."""

    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    strategy_key: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    intent: Mapped[str] = mapped_column(String(16))      # enter_long | exit_long | ...
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    perm_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # submitting | working | filled | cancelled | rejected
    status: Mapped[str] = mapped_column(String(12), default="submitting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

Create `app/engine/order_store.py`:

```python
"""Durable order-intent store for idempotent submission + reconnect dedupe."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import OrderIntent

_OPEN = ("submitting", "working")


def new_ref(strategy_key: str, symbol: str) -> str:
    return f"{strategy_key}-{symbol}-{uuid4().hex[:8]}"


async def record_intent(strategy_key: str, symbol: str, intent: str, quantity: int) -> str:
    ref = new_ref(strategy_key, symbol)
    async with session_scope() as s:
        s.add(OrderIntent(client_order_ref=ref, strategy_key=strategy_key, symbol=symbol,
                          intent=intent, quantity=int(quantity), status="submitting"))
    return ref


async def mark_perm_id(ref: str, perm_id: str) -> None:
    async with session_scope() as s:
        row = (await s.execute(
            select(OrderIntent).where(OrderIntent.client_order_ref == ref))).scalars().first()
        if row is not None:
            row.perm_id = str(perm_id)


async def set_status(ref: str, status: str) -> None:
    async with session_scope() as s:
        row = (await s.execute(
            select(OrderIntent).where(OrderIntent.client_order_ref == ref))).scalars().first()
        if row is not None:
            row.status = status


async def open_refs() -> list[str]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(OrderIntent).where(OrderIntent.status.in_(_OPEN)))).scalars().all()
        return [r.client_order_ref for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_ord.db ../.venv/bin/python -m tests.test_order_store && rm -f _t_ord.db`
Expected: `ALL ORDER-STORE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/engine/order_store.py backend/tests/test_order_store.py
git commit -m "feat(engine): durable OrderIntent store (idempotent order refs)"
```

---

### Task 7: Wire idempotent ref into live submission

**Files:**
- Modify: `backend/app/engine/execution.py` (`submit_equity_order` live branch)
- Test: `backend/tests/test_submit_idempotent.py`

**Interfaces:**
- Consumes: `record_intent`, `mark_perm_id` (Task 6); safe-mode gate (Task 5).
- Produces: when `ENABLE_LIVE_ORDERS` is on and not safe-mode, `submit_equity_order` calls `record_intent(...)` BEFORE building/transmitting the IB order, sets `order.orderRef = ref`, stores the returned `permId` via `mark_perm_id`, and includes `"ref": ref` in its return dict. Paper/safe-mode/zero-qty branches are unchanged except they do not create intents.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_submit_idempotent.py
"""Live equity submit records a durable intent + sets orderRef before transmit.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_idem.db ENABLE_LIVE_ORDERS=1 python -m tests.test_submit_idempotent
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db
        from app.engine import execution
        from app.engine.order_store import open_refs

        get_settings().enable_live_orders = True
        await init_db()

        captured = {}

        class FakeOrder:
            def __init__(self, *a, **k): self.orderRef = None

        class FakeTrade:
            class order:  # noqa: N801
                permId = 555
            class orderStatus:  # noqa: N801
                status = "Submitted"

        class FakeIB:
            def qualifyContractsAsync(self, *a): 
                async def _n(): return a
                return _n()
            def placeOrder(self, contract, order):
                captured["orderRef"] = order.orderRef
                return FakeTrade()

        async def fake_get_ib(): return FakeIB()
        execution.get_gateway().get_ib = fake_get_ib  # type: ignore[assignment]
        execution.MarketOrder = FakeOrder              # type: ignore[attr-defined]

        res = await execution.submit_equity_order("AAPL", "enter_long", 10, reason="entry")
        assert res["transmitted"] is True, res
        assert res["ref"] in (await _refs_then(open_refs)), res
        assert captured["orderRef"] == res["ref"], captured
        print("PASS: live submit set orderRef + recorded intent")

    async def _refs_then(fn): return await fn()
    asyncio.run(_t())
    print("\nALL SUBMIT-IDEMPOTENT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_idem.db ENABLE_LIVE_ORDERS=1 ../.venv/bin/python -m tests.test_submit_idempotent`
Expected: FAIL — `KeyError: 'ref'` (return dict has no ref yet).

- [ ] **Step 3: Implement**

**FIRST read `app/engine/execution.py:27` (`submit_equity_order`) end-to-end** to confirm the live branch's actual names — the order class it constructs (`MarketOrder`/`LimitOrder`/bracket), the IB call (`placeOrder` vs `placeOrderAsync`), and the returned dict's existing keys. Adjust the test in Step 1 and the code below to match those real names if they differ from the assumptions used here.

In `submit_equity_order`, locate the live branch (after the `enable_live_orders` check passes, where it qualifies the contract and places the order). Before constructing/placing the order add:

```python
    from app.engine.order_store import mark_perm_id, record_intent
    ref = await record_intent("", symbol, intent, quantity)
```

Set the ref on the order object before `placeOrder` (e.g. `order.orderRef = ref`), then after `trade = ib.placeOrder(contract, order)`:

```python
    perm = getattr(getattr(trade, "order", None), "permId", None)
    if perm:
        await mark_perm_id(ref, str(perm))
```

Add `"ref": ref` to the returned dict on the transmitted path:

```python
    return {"transmitted": True, "ref": ref,
            "detail": f"LIVE {action} {symbol} x{quantity}", ...}
```

(Note: `record_intent`'s first arg is `strategy_key`; pass `""` here since `submit_equity_order` is not strategy-aware. A follow-up may thread the key through; not required for Phase 1 dedupe, which keys on ref/permId.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_idem.db ENABLE_LIVE_ORDERS=1 ../.venv/bin/python -m tests.test_submit_idempotent && rm -f _t_idem.db`
Expected: `ALL SUBMIT-IDEMPOTENT TESTS PASSED`.

**Regression (shared file `execution.py`):** also re-run
`DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_execution_ibkr && rm -f _t.db` and confirm it still passes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/execution.py backend/tests/test_submit_idempotent.py
git commit -m "feat(engine): live equity submit records durable orderRef before transmit"
```

---

### Task 8: Reconciliation snapshots (broker truth fetch)

**Files:**
- Create: `backend/app/engine/reconciliation.py`
- Test: `backend/tests/test_recon_snapshot.py`

**Interfaces:**
- Produces: dataclasses `BrokerPosition(symbol, qty: int, avg_price: float)`, `BrokerFill(symbol, qty: int, price: float, perm_id: str | None, exec_id: str)`, `BrokerOrder(symbol, perm_id: str | None, order_ref: str | None, status: str)`; `async fetch_snapshot(ib) -> dict` returning `{"positions": [...], "fills": [...], "orders": [...]}` built from `ib.reqPositionsAsync()`, `ib.reqExecutionsAsync(ExecutionFilter())`, `ib.reqAllOpenOrdersAsync()`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_recon_snapshot.py
"""fetch_snapshot normalizes IBKR positions/fills/orders.
   Run: python -m tests.test_recon_snapshot
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.engine.reconciliation import fetch_snapshot

    class C:
        def __init__(self, sym): self.symbol = sym; self.secType = "STK"
    class Pos:
        def __init__(self, sym, q, c): self.contract = C(sym); self.position = q; self.avgCost = c
    class Exec:
        def __init__(self): self.shares = 5; self.price = 101.0; self.permId = 7; self.execId = "e1"
    class Fill:
        def __init__(self, sym): self.contract = C(sym); self.execution = Exec()
    class Order:
        def __init__(self): self.permId = 7; self.orderRef = "strategy_1-AAPL-abc"
    class OrderStatus:
        status = "Submitted"
    class Trade:
        def __init__(self, sym): self.contract = C(sym); self.order = Order(); self.orderStatus = OrderStatus()

    class FakeIB:
        async def reqPositionsAsync(self): return [Pos("AAPL", 10, 1000.0)]
        async def reqExecutionsAsync(self, flt): return [Fill("AAPL")]
        async def reqAllOpenOrdersAsync(self): return [Trade("AAPL")]

    async def _t():
        snap = await fetch_snapshot(FakeIB())
        assert snap["positions"][0].symbol == "AAPL" and snap["positions"][0].qty == 10
        assert snap["fills"][0].qty == 5 and snap["fills"][0].perm_id == "7"
        assert snap["orders"][0].order_ref == "strategy_1-AAPL-abc"
    asyncio.run(_t())
    print("PASS: snapshot normalized")
    print("\nALL RECON-SNAPSHOT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_recon_snapshot`
Expected: FAIL — `ModuleNotFoundError: app.engine.reconciliation`.

- [ ] **Step 3: Implement**

```python
# backend/app/engine/reconciliation.py
"""Reconcile broker truth (positions/fills/open orders) against local records."""
from __future__ import annotations

from dataclasses import dataclass

from ib_async import ExecutionFilter


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_price: float


@dataclass
class BrokerFill:
    symbol: str
    qty: int
    price: float
    perm_id: str | None
    exec_id: str


@dataclass
class BrokerOrder:
    symbol: str
    perm_id: str | None
    order_ref: str | None
    status: str


async def fetch_snapshot(ib) -> dict:
    positions = [
        BrokerPosition(p.contract.symbol, int(p.position), float(p.avgCost or 0.0))
        for p in await ib.reqPositionsAsync() if int(p.position) != 0
    ]
    fills = []
    for f in await ib.reqExecutionsAsync(ExecutionFilter()):
        ex = f.execution
        fills.append(BrokerFill(f.contract.symbol, int(ex.shares), float(ex.price),
                                str(ex.permId) if ex.permId else None, ex.execId))
    orders = [
        BrokerOrder(t.contract.symbol,
                    str(t.order.permId) if t.order.permId else None,
                    t.order.orderRef or None, t.orderStatus.status)
        for t in await ib.reqAllOpenOrdersAsync()
    ]
    return {"positions": positions, "fills": fills, "orders": orders}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_recon_snapshot`
Expected: `ALL RECON-SNAPSHOT TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/reconciliation.py backend/tests/test_recon_snapshot.py
git commit -m "feat(engine): reconciliation snapshot (positions/fills/orders)"
```

---

### Task 9: Reconciliation matrix evaluation

**Files:**
- Modify: `backend/app/engine/reconciliation.py`
- Test: `backend/tests/test_recon_matrix.py`

**Interfaces:**
- Consumes: `BrokerPosition`, `OrderIntent` (via `order_store.open_refs` / perm ids), `TradeRecord`.
- Produces: `reconcile(broker_positions: list[BrokerPosition], local_open: dict[str, int]) -> ReconResult` where `local_open` maps symbol → signed qty the system believes it holds; `ReconResult(ok: bool, discrepancies: list[str])`. `ok` is False (→ caller trips SAFE_MODE) when any symbol has a broker position with no/again-mismatched local belief, per the spec matrix. Pure function (no I/O) for table-testing.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_recon_matrix.py
"""Reconciliation matrix: each broker×local cell -> ok or discrepancy.
   Run: python -m tests.test_recon_matrix
"""
from __future__ import annotations


def _run() -> None:
    from app.engine.reconciliation import BrokerPosition, reconcile

    P = BrokerPosition
    # broker flat + local flat
    assert reconcile([], {}).ok is True
    # broker matches local
    assert reconcile([P("AAPL", 10, 100.0)], {"AAPL": 10}).ok is True
    # broker has unknown position, local flat -> NOT ok
    r = reconcile([P("AAPL", 10, 100.0)], {})
    assert r.ok is False and any("AAPL" in d for d in r.discrepancies), r
    # qty mismatch -> NOT ok
    assert reconcile([P("AAPL", 10, 100.0)], {"AAPL": 5}).ok is False
    # local thinks open, broker flat -> NOT ok (could be a missed close; caller decides)
    assert reconcile([], {"AAPL": 10}).ok is False
    print("PASS: matrix cells evaluated")
    print("\nALL RECON-MATRIX TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_recon_matrix`
Expected: FAIL — `ImportError: cannot import name 'reconcile'`.

- [ ] **Step 3: Implement**

Append to `reconciliation.py`:

```python
@dataclass
class ReconResult:
    ok: bool
    discrepancies: list[str]


def reconcile(broker_positions: list[BrokerPosition], local_open: dict[str, int]) -> ReconResult:
    discrepancies: list[str] = []
    broker = {p.symbol: p.qty for p in broker_positions}
    for sym, bqty in broker.items():
        lqty = local_open.get(sym, 0)
        if lqty != bqty:
            discrepancies.append(f"{sym}: broker {bqty} vs local {lqty}")
    for sym, lqty in local_open.items():
        if lqty != 0 and sym not in broker:
            discrepancies.append(f"{sym}: local {lqty} vs broker flat")
    return ReconResult(ok=not discrepancies, discrepancies=discrepancies)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_recon_matrix`
Expected: `ALL RECON-MATRIX TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/reconciliation.py backend/tests/test_recon_matrix.py
git commit -m "feat(engine): reconciliation matrix evaluation"
```

---

### Task 10: Durable worker state (table + store)

**Files:**
- Modify: `backend/app/db/models.py` (add `WorkerState`)
- Create: `backend/app/engine/worker_state.py`
- Test: `backend/tests/test_worker_state.py`

**Interfaces:**
- Produces: model `WorkerState(strategy_key pk, managed_symbol, armed: dict|None JSON, open_trade_id: int|None, last_bar_ms: int|None, updated_at)`; `async save_state(strategy_key, *, managed_symbol=None, armed=None, open_trade_id=None, last_bar_ms=None) -> None` (upsert); `async load_state(strategy_key) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_worker_state.py
"""Worker state snapshot round-trips across a simulated restart.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_ws.db python -m tests.test_worker_state
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.worker_state import load_state, save_state

        await init_db()
        assert await load_state("strategy_1") is None
        await save_state("strategy_1", managed_symbol="AAPL",
                         armed={"side": "long", "price": 100.0, "stop": 99.0, "target": 102.0},
                         open_trade_id=42, last_bar_ms=1700000000000)
        snap = await load_state("strategy_1")
        assert snap["managed_symbol"] == "AAPL" and snap["open_trade_id"] == 42
        assert snap["armed"]["target"] == 102.0
        # upsert: update one field, others persist
        await save_state("strategy_1", last_bar_ms=1700000300000)
        snap = await load_state("strategy_1")
        assert snap["last_bar_ms"] == 1700000300000 and snap["managed_symbol"] == "AAPL"
        print("PASS: worker-state upsert round-trip")
    asyncio.run(_t())
    print("\nALL WORKER-STATE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_ws.db ../.venv/bin/python -m tests.test_worker_state`
Expected: FAIL — `ModuleNotFoundError: app.engine.worker_state`.

- [ ] **Step 3: Implement model + store**

Add to `models.py` after `OrderIntent`:

```python
class WorkerState(Base):
    """Durable snapshot of a worker's in-memory State so a restart resumes mid-trade."""

    __tablename__ = "worker_state"

    strategy_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    managed_symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    armed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    open_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_bar_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
```

Create `app/engine/worker_state.py`:

```python
"""Durable per-strategy worker state (managed symbol, armed entry, open trade)."""
from __future__ import annotations

from app.db.base import session_scope
from app.db.models import WorkerState

_SENTINEL = object()


async def save_state(strategy_key: str, *, managed_symbol=_SENTINEL, armed=_SENTINEL,
                     open_trade_id=_SENTINEL, last_bar_ms=_SENTINEL) -> None:
    async with session_scope() as s:
        row = await s.get(WorkerState, strategy_key)
        if row is None:
            row = WorkerState(strategy_key=strategy_key)
            s.add(row)
        if managed_symbol is not _SENTINEL:
            row.managed_symbol = managed_symbol
        if armed is not _SENTINEL:
            row.armed = armed
        if open_trade_id is not _SENTINEL:
            row.open_trade_id = open_trade_id
        if last_bar_ms is not _SENTINEL:
            row.last_bar_ms = last_bar_ms


async def load_state(strategy_key: str) -> dict | None:
    async with session_scope() as s:
        row = await s.get(WorkerState, strategy_key)
        if row is None:
            return None
        return {"managed_symbol": row.managed_symbol, "armed": row.armed,
                "open_trade_id": row.open_trade_id, "last_bar_ms": row.last_bar_ms}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_ws.db ../.venv/bin/python -m tests.test_worker_state && rm -f _t_ws.db`
Expected: `ALL WORKER-STATE TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/engine/worker_state.py backend/tests/test_worker_state.py
git commit -m "feat(engine): durable WorkerState table + store"
```

---

### Task 11: RecoveryCoordinator (startup/reconnect sequence)

**Files:**
- Create: `backend/app/engine/recovery.py`
- Test: `backend/tests/test_recovery_sequence.py`

**Interfaces:**
- Consumes: `fetch_snapshot`, `reconcile`, `trip_safe_mode`, `is_safe_mode`, `Gateway.set_state`, `ConnState`, `TradeRecord` (to compute `local_open`).
- Produces: `async local_open_positions() -> dict[str, int]` (signed qty per symbol from open `TradeRecord`s); `async run_recovery(ib, gateway) -> bool` — sets `RECOVERING`, fetches snapshot, reconciles vs `local_open_positions()`; on mismatch trips SAFE_MODE + sets `SAFE_MODE` state + returns False; else sets `READY` + returns True. Idempotent and side-effect-safe to call on every CONNECTED transition.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_recovery_sequence.py
"""Recovery: clean broker state -> READY; orphan position -> SAFE_MODE.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rec.db python -m tests.test_recovery_sequence
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.recovery import run_recovery
        from app.engine.safe_mode import is_safe_mode
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()

        class C:
            def __init__(self, sym): self.symbol = sym; self.secType = "STK"
        class Pos:
            def __init__(self, sym, q): self.contract = C(sym); self.position = q; self.avgCost = 100.0

        class CleanIB:
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []
        class OrphanIB(CleanIB):
            async def reqPositionsAsync(self): return [Pos("AAPL", 10)]

        gw = Gateway()
        ok = await run_recovery(CleanIB(), gw)
        assert ok is True and gw.state is ConnState.READY, gw.state
        assert await is_safe_mode() is False
        print("PASS: clean recovery -> READY")

        gw2 = Gateway()
        ok2 = await run_recovery(OrphanIB(), gw2)
        assert ok2 is False and gw2.state is ConnState.SAFE_MODE, gw2.state
        assert await is_safe_mode() is True
        print("PASS: orphan position -> SAFE_MODE")

    asyncio.run(_t())
    print("\nALL RECOVERY TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_rec.db ../.venv/bin/python -m tests.test_recovery_sequence`
Expected: FAIL — `ModuleNotFoundError: app.engine.recovery`.

- [ ] **Step 3: Implement**

```python
# backend/app/engine/recovery.py
"""Deterministic recovery sequence run on every transition into CONNECTED."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import TradeRecord
from app.engine.reconciliation import fetch_snapshot, reconcile
from app.engine.safe_mode import trip_safe_mode
from app.ibkr.gateway import ConnState
from app.services.journal import log_event

logger = logging.getLogger("trading.recovery")


async def local_open_positions() -> dict[str, int]:
    out: dict[str, int] = {}
    async with session_scope() as s:
        rows = (await s.execute(
            select(TradeRecord).where(TradeRecord.status == "open"))).scalars().all()
        for r in rows:
            signed = int(r.quantity) * (1 if (r.side or "").lower() in ("buy", "long") else -1)
            out[r.symbol] = out.get(r.symbol, 0) + signed
    return {k: v for k, v in out.items() if v != 0}


async def run_recovery(ib, gateway) -> bool:
    gateway.set_state(ConnState.RECOVERING)
    snap = await fetch_snapshot(ib)
    result = reconcile(snap["positions"], await local_open_positions())
    if not result.ok:
        reason = "; ".join(result.discrepancies)
        await trip_safe_mode(reason)
        gateway.set_state(ConnState.SAFE_MODE)
        await log_event(f"SAFE MODE on recovery: {reason}", level="error",
                        category="system")
        return False
    gateway.set_state(ConnState.READY)
    await log_event("Recovery clean — READY", category="system")
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_rec.db ../.venv/bin/python -m tests.test_recovery_sequence && rm -f _t_rec.db`
Expected: `ALL RECOVERY TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/recovery.py backend/tests/test_recovery_sequence.py
git commit -m "feat(engine): RecoveryCoordinator sequence (reconcile -> READY/SAFE_MODE)"
```

---

### Task 12: Workers pause unless READY

**Files:**
- Modify: `backend/app/engine/worker.py` (rule-driven + multi-position loops)
- Test: `backend/tests/test_worker_pause.py`

**Interfaces:**
- Consumes: `get_gateway().state`, `ConnState`.
- Produces: a guard helper `_trading_allowed() -> bool` (`get_gateway().state is ConnState.READY`) checked at the top of each worker cycle; when not allowed the worker sets status `paused`, sleeps `scan_interval_seconds`, and skips scan/decide/order without raising.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_worker_pause.py
"""Worker does not act while connection state is not READY.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_wp.db python -m tests.test_worker_pause
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import StrategyState
        from app.engine.manager import engine_manager
        from app.engine.worker import run_strategy
        from app.ibkr.gateway import ConnState, get_gateway
        from app.strategies.base import BaseStrategy, Candidate

        get_settings().scan_interval_seconds = 0
        get_gateway().set_state(ConnState.DISCONNECTED)   # NOT ready
        await init_db()
        async with session_scope() as s:
            s.add(StrategyState(key="paws", name="P", ready=True, status="idle"))

        scanned = {"n": 0}

        class S(BaseStrategy):
            key = "paws"; name = "P"; ready = True
            async def scan(self, universe):
                scanned["n"] += 1
                return [Candidate(symbol="AAPL", score=1.0, reason="x")]

        await engine_manager.start_worker("paws", run_strategy(S()))
        await asyncio.sleep(0.3)
        assert scanned["n"] == 0, f"worker scanned while not READY: {scanned}"
        async with session_scope() as s:
            st = await s.get(StrategyState, "paws")
            assert st.status == "paused", st.status
        await engine_manager.kill_all()
        print("PASS: worker paused while not READY")
    asyncio.run(_t())
    print("\nALL WORKER-PAUSE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_wp.db ../.venv/bin/python -m tests.test_worker_pause`
Expected: FAIL — worker scans anyway (`scanned['n'] > 0`).

- [ ] **Step 3: Implement**

**FIRST read `app/engine/worker.py` in full — especially `_run_multi_position`, whose body is not shown here.** Match its actual loop structure (it may not be a bare `while True:` and may name its status helper differently); apply the guard to match.

At the top of `worker.py` add:

```python
from app.ibkr.gateway import ConnState, get_gateway


def _trading_allowed() -> bool:
    return get_gateway().state is ConnState.READY
```

In `_run_rule_driven`'s `while True:` loop, make the first lines:

```python
            if not _trading_allowed():
                await _set_status(strategy.key, "paused")
                await asyncio.sleep(settings.scan_interval_seconds)
                continue
```

Add the same guard as the first lines inside the multi-position loop (`_run_multi_position`), matching its real structure. (Use the existing `_set_status` helper already in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_wp.db ../.venv/bin/python -m tests.test_worker_pause && rm -f _t_wp.db`
Expected: `ALL WORKER-PAUSE TESTS PASSED`.

**Regression (shared file `worker.py`):** the existing `test_engine_worker` sets no connection
state, so add `get_gateway().set_state(ConnState.READY)` near the top of that test's `_run()` (it
asserts the worker scans), then run `DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_engine_worker && rm -f _t.db` and confirm it still passes. Commit that one-line test
update with this task.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/worker.py backend/tests/test_worker_pause.py
git commit -m "feat(engine): workers pause unless connection state is READY"
```

---

### Task 13: App startup runs recovery; reconnect re-runs it

**Files:**
- Modify: `backend/app/ibkr/gateway.py` (call recovery on CONNECTED; start heartbeat+reconnect tasks)
- Modify: `backend/app/main.py` (startup uses the recovery path)
- Test: `backend/tests/test_startup_recovery.py`

**Interfaces:**
- Consumes: `run_recovery` (Task 11), `reconnect_loop` (Task 3), `heartbeat_once` (Task 4).
- Produces: `async Gateway.connect_and_recover() -> bool` — `connect()` then `run_recovery(self._ib, self)`; returns the recovery result. App startup calls `connect_and_recover()` (best-effort: logs and continues if the Gateway is down, leaving state DISCONNECTED so workers stay paused). A background `Gateway.supervise()` task (heartbeat every N seconds; on DISCONNECTED → `reconnect_loop()` then `run_recovery`) is started at startup. To avoid an import cycle, `run_recovery` is imported lazily inside the method.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_startup_recovery.py
"""connect_and_recover connects then runs recovery to READY on clean state.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_sr.db python -m tests.test_startup_recovery
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
            def reqMarketDataType(self, *_): pass
            async def connectAsync(self, *a, **k): return self
            def __iadd__(self, *_): return self
            # events used by _wire_events:
            disconnectedEvent = property(lambda self: _Ev())
            errorEvent = property(lambda self: _Ev())
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        class _Ev:
            def __iadd__(self, h): return self

        gw = Gateway(ib_factory=CleanIB)
        ok = await gw.connect_and_recover()
        assert ok is True and gw.state is ConnState.READY, gw.state
        print("PASS: startup connect_and_recover -> READY")
    asyncio.run(_t())
    print("\nALL STARTUP-RECOVERY TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_sr.db ../.venv/bin/python -m tests.test_startup_recovery`
Expected: FAIL — `AttributeError: ... 'connect_and_recover'`.

- [ ] **Step 3: Implement**

Add to `Gateway`:

```python
    async def connect_and_recover(self) -> bool:
        from app.engine.recovery import run_recovery  # lazy: avoid import cycle
        await self.connect()
        return await run_recovery(self._ib, self)

    async def supervise(self, interval: float = 15.0) -> None:
        while True:
            await asyncio.sleep(interval)
            if self.state in (ConnState.DISCONNECTED,):
                await self.reconnect_loop()
                from app.engine.recovery import run_recovery
                await run_recovery(self._ib, self)
            elif self.is_connected():
                await self.heartbeat_once()
```

In `app/main.py` startup (where it currently connects / notes "App boots without a live connection"), replace the bare connect with a best-effort recovery + supervisor launch:

```python
    from app.ibkr.gateway import get_gateway
    gw = get_gateway()
    try:
        await gw.connect_and_recover()
    except Exception:  # noqa: BLE001 — boot even if the Gateway is down
        logger.warning("IBKR not connected at startup; workers stay paused until READY")
    import asyncio
    asyncio.create_task(gw.supervise())
```

(Verify the exact startup hook in `main.py` — it uses a FastAPI lifespan/startup; add the above inside it, after `init_db()`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_sr.db ../.venv/bin/python -m tests.test_startup_recovery && rm -f _t_sr.db`
Expected: `ALL STARTUP-RECOVERY TESTS PASSED`.

- [ ] **Step 5: Full suite + commit**

```bash
cd backend
for t in tests/test_*.py; do DATABASE_URL=sqlite+aiosqlite:///./_p.db ../.venv/bin/python -m "tests.$(basename "$t" .py)" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done; rm -f _p.db
git add backend/app/ibkr/gateway.py backend/app/main.py backend/tests/test_startup_recovery.py
git commit -m "feat(ibkr): run recovery on startup + supervisor (heartbeat/reconnect)"
```

Expected: every resilience test prints `ok`; pre-existing `test_direction_risk`/`test_optimizer` may remain `FAIL` (documented, unrelated).

---

### Task 14: Rule-driven worker persists durable state on transitions

**Files:**
- Modify: `backend/app/engine/worker.py` (`_handle_action` in the rule-driven path)
- Test: `backend/tests/test_worker_state_writes.py`

**Interfaces:**
- Consumes: `save_state` (Task 10), `State`/`Pending`/`Position` (`ema_scalper.signals`).
- Produces: after each handled action in `_handle_action`, the worker persists a `WorkerState`
  snapshot — `managed_symbol=symbol`; `armed` = the pending entry as a dict
  (`{"side","trigger","stop","target","entry_type"}`) or `None`; `open_trade_id`; `last_bar_ms`
  = `bar.get("time")`. Only the rule-driven path is touched (the multi-position path already
  reconstructs from the DB via `_reconstruct_positions`).

> **FIRST read `app/engine/worker.py` `_handle_action` and `_run_rule_driven` end-to-end** (lines
> ~50–166) and `ema_scalper/signals.py` `State`/`Pending`/`Position` (shown in the plan context)
> to match field names exactly.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_worker_state_writes.py
"""Rule-driven worker persists a WorkerState snapshot when it arms an entry.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_wsw.db python -m tests.test_worker_state_writes
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine import worker as W
        from app.engine.worker_state import load_state
        from app.strategies.ema_scalper.signals import Action, State

        await init_db()

        class Strat:
            key = "strategy_1"

        state = State()
        bars = [{"time": 1700000000000, "high": 101, "low": 99, "close": 100}]
        action = Action(kind="arm", side="long", price=101.0, stop=99.0, target=103.0,
                        entry_type="pin")
        # apply_action passthrough that just sets pending so persistence has something to read
        def apply_action(a, st, idx, bar):
            from app.strategies.ema_scalper.signals import Pending
            st.pending = Pending(side=a.side, trigger=a.price, stop=a.stop, target=a.target,
                                 armed_index=idx, entry_type=a.entry_type)

        await W._handle_action(Strat(), "AAPL", action, state, bars, 0, None,
                               size_position=None, submit_equity_order=None,
                               apply_action=apply_action)
        snap = await load_state("strategy_1")
        assert snap is not None and snap["managed_symbol"] == "AAPL", snap
        assert snap["armed"]["side"] == "long" and snap["armed"]["trigger"] == 101.0, snap
        print("PASS: arm action persisted to WorkerState")
    asyncio.run(_t())
    print("\nALL WORKER-STATE-WRITE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_wsw.db ../.venv/bin/python -m tests.test_worker_state_writes`
Expected: FAIL — `assert snap is not None` (nothing persisted yet).

- [ ] **Step 3: Implement**

In `worker.py` add a helper and call it at the end of `_handle_action`, just before each `return`:

```python
async def _persist_worker_state(strategy_key, symbol, state, open_trade_id, last_bar_ms):
    from app.engine.worker_state import save_state
    p = state.pending
    armed = None if p is None else {"side": p.side, "trigger": p.trigger, "stop": p.stop,
                                    "target": p.target, "entry_type": p.entry_type}
    await save_state(strategy_key, managed_symbol=symbol, armed=armed,
                     open_trade_id=open_trade_id, last_bar_ms=last_bar_ms)
```

At the end of `_handle_action`, replace the bare `return open_qty, open_trade_id` paths so each
first persists. The simplest correct approach: compute the return value, persist, then return.
For the arm/cancel branches (which fall through to the final `return open_qty, open_trade_id`),
add before that final return:

```python
    await _persist_worker_state(strategy.key, symbol, state, open_trade_id, bar.get("time"))
    return open_qty, open_trade_id
```

For the `enter` branch, before `return sizing.quantity, trade_id` add:

```python
    await _persist_worker_state(strategy.key, symbol, state, trade_id, bar.get("time"))
```

For the `exit` branch, before `return 0, None` add:

```python
    await _persist_worker_state(strategy.key, symbol, state, None, bar.get("time"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_wsw.db ../.venv/bin/python -m tests.test_worker_state_writes && rm -f _t_wsw.db`
Expected: `ALL WORKER-STATE-WRITE TESTS PASSED`.

**Regression (shared file `worker.py`):** re-run `test_engine_worker` (with the `READY` set-state
line added in Task 12) and confirm it still passes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/worker.py backend/tests/test_worker_state_writes.py
git commit -m "feat(engine): rule-driven worker persists durable state on transitions"
```

---

### Task 15: Rule-driven worker rebuilds state on startup (resume mid-trade)

**Files:**
- Modify: `backend/app/engine/worker.py` (`_run_rule_driven` startup)
- Test: `backend/tests/test_worker_state_rebuild.py`

**Interfaces:**
- Consumes: `load_state` (Task 10), open `TradeRecord` (for the position), `State`/`Pending`/
  `Position` (`ema_scalper.signals`).
- Produces: at `_run_rule_driven` start, if `load_state(strategy.key)` returns a snapshot,
  rebuild `state.pending` from `armed` and `state.position` from the open `TradeRecord` named by
  `open_trade_id`, and seed `selected`, `open_qty`, `open_trade_id`, `last_bar_ms`. The
  **broker cross-check is already provided by Task 11's reconcile** (open `TradeRecord`s vs broker
  positions trips SAFE_MODE before workers reach READY), so rebuild trusts the open trade.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_worker_state_rebuild.py
"""Rule-driven startup rebuilds in-memory State from a persisted snapshot.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_wsr.db python -m tests.test_worker_state_rebuild
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine.worker import _rebuild_rule_state
        from app.engine.worker_state import save_state

        await init_db()
        async with session_scope() as s:
            tr = TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                             quantity=10, entry_price=100.0, stop=99.0, target=103.0,
                             status="open")
            s.add(tr)
            await s.flush()
            tid = tr.id
        await save_state("strategy_1", managed_symbol="AAPL", armed=None,
                         open_trade_id=tid, last_bar_ms=1700000000000)

        rebuilt = await _rebuild_rule_state("strategy_1")
        assert rebuilt is not None, "expected a rebuilt snapshot"
        state, selected, open_qty, open_trade_id, last_bar_ms = rebuilt
        assert selected == "AAPL" and open_qty == 10 and open_trade_id == tid, rebuilt
        assert state.position is not None and state.position.side == "long", state.position
        assert state.position.stop == 99.0 and state.position.target == 103.0
        print("PASS: rebuilt open position from snapshot + trade record")
    asyncio.run(_t())
    print("\nALL WORKER-STATE-REBUILD TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_wsr.db ../.venv/bin/python -m tests.test_worker_state_rebuild`
Expected: FAIL — `AttributeError: ... '_rebuild_rule_state'`.

- [ ] **Step 3: Implement**

Add to `worker.py`:

```python
async def _rebuild_rule_state(strategy_key: str):
    """Rebuild (state, selected, open_qty, open_trade_id, last_bar_ms) from the durable
    snapshot + the open trade. Returns None when there's nothing to resume."""
    from app.db.base import session_scope
    from app.db.models import TradeRecord
    from app.strategies.ema_scalper.signals import Pending, Position, State

    snap = await _load_worker_snapshot(strategy_key)
    if snap is None:
        return None
    state = State()
    selected = snap["managed_symbol"]
    open_qty, open_trade_id = 0, snap["open_trade_id"]
    if snap["armed"]:
        a = snap["armed"]
        state.pending = Pending(side=a["side"], trigger=a["trigger"], stop=a["stop"],
                                target=a["target"], armed_index=0, entry_type=a.get("entry_type", ""))
    if open_trade_id is not None:
        async with session_scope() as s:
            tr = await s.get(TradeRecord, open_trade_id)
            if tr is not None and tr.status == "open":
                side = "long" if (tr.side or "").lower() in ("buy", "long") else "short"
                state.position = Position(side=side, entry=tr.entry_price or 0.0,
                                          stop=tr.stop or 0.0, target=tr.target or 0.0,
                                          entry_index=0)
                open_qty = int(tr.quantity)
    return state, selected, open_qty, open_trade_id, snap["last_bar_ms"]


async def _load_worker_snapshot(strategy_key: str):
    from app.engine.worker_state import load_state
    return await load_state(strategy_key)
```

In `_run_rule_driven`, replace the initial `state = State()` / `selected = None` / `open_qty = 0`
/ `open_trade_id = None` / `last_bar_ms = None` block with a rebuild attempt:

```python
    from app.strategies.ema_scalper.signals import State
    rebuilt = await _rebuild_rule_state(strategy.key)
    if rebuilt is not None:
        state, selected, open_qty, open_trade_id, last_bar_ms = rebuilt
        await log_event(f"Resumed state: {selected} pos={state.position is not None}",
                        category="action", strategy_key=strategy.key)
    else:
        state, selected, open_qty, open_trade_id, last_bar_ms = State(), None, 0, None, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_wsr.db ../.venv/bin/python -m tests.test_worker_state_rebuild && rm -f _t_wsr.db`
Expected: `ALL WORKER-STATE-REBUILD TESTS PASSED`.

**Regression (shared file `worker.py`):** re-run `test_engine_worker` and `test_worker_pause` and
confirm both still pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/worker.py backend/tests/test_worker_state_rebuild.py
git commit -m "feat(engine): rule-driven worker resumes in-memory state on restart"
```

---

## Manual smoke (live, after the suite is green)

With the paper IB Gateway connected during RTH:

```bash
cd backend && ../.venv/bin/python -m scripts.connect_smoke   # confirms connect+recovery path live
```

Disconnect the Gateway mid-run (or stop/restart it) and confirm the logs show: state → DISCONNECTED, reconnect attempts with backoff, then a recovery pass → READY (or SAFE_MODE with a discrepancy report if a position can't be matched).

## Durable strategy state (folded into Phase 1)

Per the requester, durable strategy state is **fully in Phase 1**: Task 10 lands the persistence
layer (`WorkerState` table + store), Task 14 wires the **write-on-transition hooks** (the
rule-driven worker persists on arm/enter/exit/cancel), and Task 15 wires the **rebuild on startup**
(resume mid-trade). The **broker cross-check** is provided by Task 11's reconcile — open
`TradeRecord`s are reconciled against broker positions and trip SAFE_MODE before any worker reaches
READY, so the rebuild trusts the open trade. Only the **rule-driven** path (e.g. `strategy_1`
scalper) needs this; the **multi-position** crossover already reconstructs from the DB via
`_reconstruct_positions`.

## Architectural note — `get_ib()` lazy reconnect (Phase 2)

`Gateway.get_ib()` still lazily auto-reconnects for any caller (`market.py`, `account.py`,
`execution.py`). After Phase 1 that means a caller can reconnect the socket *without* running
the recovery sequence — producing "connected but state ≠ READY" and a possible race with
`supervise()`'s reconnect. The Phase 1 safety guarantee still holds (workers are gated on READY
in Task 12; orders are gated by safe-mode in Task 5), so this is **not urgent** — but a
professional connection model routes *all* reconnects through the supervisor + recovery. Track
as a Phase 2 item: make `get_ib()` on a dead socket request a supervised recovery rather than a
bare reconnect.

## Out of scope (Phase 2 — separate plan)

Market-data backfill + live re-subscribe (`BackfillService`), pluggable alert channel (email/webhook),
UI surfacing of connection/safe-mode state + recovery-report view, threading `strategy_key` through
`submit_equity_order` for per-strategy intent attribution, and applying *confirmed* missed close-fills
automatically (Phase 1 trips SAFE_MODE on any open/flat mismatch rather than auto-applying).
