# IBKR Resilience — Phase 2b (Ops Visibility) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface resilience state to the operator — a connection-state badge, a SAFE_MODE banner with a review-and-Resume action, and in-app alerts on disconnect / SAFE_MODE / prolonged reconnect failure.

**Architecture:** Fold `conn_state`/`safe_mode`/`safe_mode_reason` into the status payload the frontend already polls; add a `POST /api/system/resume` (clear safe mode → re-run recovery); add a pluggable `alerts` service (default channel = journal + log) wired into gateway/recovery; add the badge + banner to the React app.

**Tech Stack:** Python 3.14, FastAPI, async SQLAlchemy, ib_async; React + Vite (no JS test runner).

**Source spec:** `docs/superpowers/specs/2026-06-29-ibkr-resilience-phase2b-design.md`

## Global Constraints

- `/api/auth/status` stays **ungated** (frontend polls it pre-auth for the badge); the resume
  endpoint is gated by `require_auth`.
- Alert channel ships **in-app only** (journal `system` event + structured log), pluggable via
  `register_channel`. `send_alert` must never raise into its caller; a failing channel is skipped.
- **Resume = `clear_safe_mode()` then `run_recovery(...)`** — reaches READY only if the broker now
  reconciles; otherwise re-trips SAFE_MODE. Never force READY.
- `conn_state` values are exactly `ConnState(...).value`:
  `disconnected|connecting|connected|recovering|ready|safe_mode`.
- Backend tasks are TDD (module-run `_run()` tests, `python -m tests.<name>`). Frontend tasks are
  verified manually (`npm run build` + visual) — no JS test runner exists.

## File structure
- Modify `app/auth/routes.py` — `ConnectionStatus` + `_status` gain resilience fields.
- Create `app/services/alerts.py` — `send_alert`/`fire_alert`/`register_channel` + default channel.
- Modify `app/ibkr/gateway.py` — `_on_disconnect` fires an alert; `reconnect_loop` alerts on threshold.
- Modify `app/engine/recovery.py` — SAFE_MODE branch sends an alert.
- Create `app/routers/system.py` — `POST /api/system/resume`; mount in `app/main.py`.
- Modify `frontend/src/api.js` — `resume()`.
- Modify `frontend/src/components/ApiTab.jsx` — badge colours by `conn_state`.
- Create `frontend/src/components/SafeModeBanner.jsx` + mount in `frontend/src/App.jsx`.

---

### Task 1: Status payload resilience fields

**Files:**
- Modify: `backend/app/auth/routes.py`
- Test: `backend/tests/test_status_resilience.py`

**Interfaces:**
- Produces: `ConnectionStatus` gains `conn_state: str = "disconnected"`, `safe_mode: bool = False`,
  `safe_mode_reason: str | None = None`. `_status()` populates them from
  `get_gateway().state.value` and the persisted safe-mode flag (`is_safe_mode()` + the
  `SystemFlag(key="safe_mode").reason`); a safe-mode read error defaults to `safe_mode=False`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_status_resilience.py
"""/api/auth/status payload exposes conn_state + safe_mode + reason.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_st.db python -m tests.test_status_resilience
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.auth.routes import _status
        from app.engine.safe_mode import trip_safe_mode
        from app.ibkr.gateway import ConnState, get_gateway

        await init_db()
        get_gateway().set_state(ConnState.SAFE_MODE)
        await trip_safe_mode("orphan position AAPL")

        st = await _status()
        assert st.conn_state == "safe_mode", st.conn_state
        assert st.safe_mode is True and st.safe_mode_reason == "orphan position AAPL", st
        print("PASS: status exposes resilience fields")
    asyncio.run(_t())
    print("\nALL STATUS-RESILIENCE TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_st.db ../.venv/bin/python -m tests.test_status_resilience`
Expected: FAIL — `AttributeError: 'ConnectionStatus' object has no attribute 'conn_state'`.

- [ ] **Step 3: Implement**

In `auth/routes.py`, add the three fields to the `ConnectionStatus` model:

```python
    conn_state: str = "disconnected"
    safe_mode: bool = False
    safe_mode_reason: str | None = None
```

Add a helper and use it in `_status()` (build the kwargs before constructing the model):

```python
async def _safe_mode_info() -> tuple[bool, str | None]:
    from app.db.base import session_scope
    from app.db.models import SystemFlag
    try:
        async with session_scope() as s:
            row = await s.get(SystemFlag, "safe_mode")
            if row and row.on:
                return True, row.reason or None
    except Exception:  # noqa: BLE001 — a status read must never 500 the badge
        logger.exception("safe-mode status read failed")
    return False, None
```

In `_status()`, before constructing `ConnectionStatus(...)`:

```python
    from app.ibkr.gateway import get_gateway
    sm_on, sm_reason = await _safe_mode_info()
```

and add to the `ConnectionStatus(...)` call:

```python
        conn_state=get_gateway().state.value, safe_mode=sm_on, safe_mode_reason=sm_reason,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_st.db ../.venv/bin/python -m tests.test_status_resilience && rm -f _t_st.db`
Expected: `ALL STATUS-RESILIENCE TESTS PASSED`.

**Regression:** `DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m pytest tests/test_auth_connection.py -q && rm -f _t.db` still passes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/auth/routes.py backend/tests/test_status_resilience.py
git commit -m "feat(api): expose conn_state + safe_mode in /api/auth/status"
```

---

### Task 2: Alerts service (pluggable, in-app default)

**Files:**
- Create: `backend/app/services/alerts.py`
- Test: `backend/tests/test_alerts.py`

**Interfaces:**
- Produces: `register_channel(ch)` where `ch` is `Callable[[str, str, dict | None], Awaitable[None]]`;
  `async send_alert(kind: str, message: str, data: dict | None = None) -> None` (dispatches to all
  channels; a raising channel is logged + skipped); `fire_alert(kind, message, data=None) -> None`
  (sync; schedules `send_alert` on the running loop, no-op if no loop). A default channel is
  registered at import that writes a `system` journal event (`level="error"` for `safe_mode`/
  `reconnect_failed`, else `"warning"`) + a logger line.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_alerts.py
"""send_alert dispatches to channels + default writes a journal event; bad channel skipped.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_al.db python -m tests.test_alerts
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import LogEntry
        from app.services import alerts
        from sqlalchemy import select

        await init_db()
        got = []
        async def good(kind, msg, data): got.append((kind, msg))
        async def bad(kind, msg, data): raise RuntimeError("boom")
        alerts.register_channel(good)
        alerts.register_channel(bad)

        await alerts.send_alert("safe_mode", "halt: orphan AAPL", {"x": 1})
        assert ("safe_mode", "halt: orphan AAPL") in got, got     # custom channel ran
        async with session_scope() as s:                          # default channel wrote a journal event
            rows = (await s.execute(select(LogEntry).where(LogEntry.category == "system"))).scalars().all()
        assert any("orphan AAPL" in r.message for r in rows), [r.message for r in rows]
        print("PASS: send_alert dispatched + journaled, bad channel skipped")
    asyncio.run(_t())
    print("\nALL ALERTS TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_al.db ../.venv/bin/python -m tests.test_alerts`
Expected: FAIL — `ModuleNotFoundError: app.services.alerts`.

- [ ] **Step 3: Implement**

```python
# backend/app/services/alerts.py
"""Pluggable operator alerts. Ships in-app only (journal + log); webhook/email can be
added later via register_channel without touching call sites. Never raises into callers."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from app.services.journal import log_event

logger = logging.getLogger("trading.alerts")

AlertChannel = Callable[[str, str, "dict | None"], Awaitable[None]]
_CHANNELS: list[AlertChannel] = []
_ERROR_KINDS = {"safe_mode", "reconnect_failed"}


def register_channel(ch: AlertChannel) -> None:
    _CHANNELS.append(ch)


async def send_alert(kind: str, message: str, data: dict | None = None) -> None:
    for ch in list(_CHANNELS):
        try:
            await ch(kind, message, data)
        except Exception:  # noqa: BLE001 — one bad channel must not block the others
            logger.exception("alert channel failed (%s)", kind)


def fire_alert(kind: str, message: str, data: dict | None = None) -> None:
    """Sync entry point for non-async callers (event handlers). Best-effort."""
    try:
        asyncio.get_running_loop().create_task(send_alert(kind, message, data))
    except RuntimeError:
        pass  # no running loop (e.g. a sync test) — skip scheduling


async def _journal_channel(kind: str, message: str, data: dict | None) -> None:
    level = "error" if kind in _ERROR_KINDS else "warning"
    await log_event(f"[{kind}] {message}", category="system", level=level, data=data)
    logger.warning("ALERT %s: %s", kind, message)


register_channel(_journal_channel)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_al.db ../.venv/bin/python -m tests.test_alerts && rm -f _t_al.db`
Expected: `ALL ALERTS TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/alerts.py backend/tests/test_alerts.py
git commit -m "feat(alerts): pluggable send_alert with in-app journal channel"
```

---

### Task 3: Wire alerts into gateway + recovery

**Files:**
- Modify: `backend/app/ibkr/gateway.py`
- Modify: `backend/app/engine/recovery.py`
- Test: `backend/tests/test_alert_wiring.py`

**Interfaces:**
- Consumes: `fire_alert`, `send_alert` (Task 2).
- Produces: `Gateway._on_disconnect` calls `fire_alert("disconnect", ...)`; `Gateway.reconnect_loop`
  calls `fire_alert("reconnect_failed", ...)` ONCE when `attempt` first reaches 5; `recovery.run_recovery`
  on the SAFE_MODE branch calls `await send_alert("safe_mode", reason, {...})` (in addition to its
  existing `log_event`/`trip_safe_mode`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_alert_wiring.py
"""Recovery SAFE_MODE sends a safe_mode alert; disconnect fires one.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_aw.db python -m tests.test_alert_wiring
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine import recovery
        from app.ibkr.gateway import ConnState, Gateway
        from app.services import alerts

        await init_db()
        seen = []
        async def cap(kind, msg, data): seen.append(kind)
        alerts.register_channel(cap)

        # An open local trade with no broker position → reconcile mismatch → SAFE_MODE.
        async with session_scope() as s:
            s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                              quantity=10, status="open"))

        class OrphanIB:
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        ok = await recovery.run_recovery(OrphanIB(), gw)
        assert ok is False and gw.state is ConnState.SAFE_MODE
        assert "safe_mode" in seen, seen

        gw._on_disconnect()
        await asyncio.sleep(0)   # let fire_alert's task run
        assert "disconnect" in seen, seen
        print("PASS: safe_mode + disconnect alerts fired")
    asyncio.run(_t())
    print("\nALL ALERT-WIRING TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_aw.db ../.venv/bin/python -m tests.test_alert_wiring`
Expected: FAIL — `assert "safe_mode" in seen` (no alert sent yet).

- [ ] **Step 3: Implement**

In `recovery.run_recovery`, on the not-ok branch, add the alert after `trip_safe_mode(reason)`:

```python
        from app.services.alerts import send_alert
        await send_alert("safe_mode", reason, {"discrepancies": result.discrepancies})
```

In `gateway.py` `_on_disconnect`:

```python
    def _on_disconnect(self, *_) -> None:
        self.set_state(ConnState.DISCONNECTED)
        from app.services.alerts import fire_alert
        fire_alert("disconnect", "IBKR Gateway disconnected")
```

In `gateway.py` `reconnect_loop`, inside the `except` block, after incrementing/at the point where
`attempt` is known, alert once at the threshold (place this right after `attempt += 1`):

```python
                if attempt == 5:
                    from app.services.alerts import fire_alert
                    fire_alert("reconnect_failed",
                               f"reconnect still failing after {attempt} attempts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_aw.db ../.venv/bin/python -m tests.test_alert_wiring && rm -f _t_aw.db`
Expected: `ALL ALERT-WIRING TESTS PASSED`.

**Regression:** `DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_recovery_sequence && rm -f _t.db` still passes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/app/engine/recovery.py backend/tests/test_alert_wiring.py
git commit -m "feat(alerts): wire disconnect / safe_mode / reconnect-failed alerts"
```

---

### Task 4: Resume endpoint

**Files:**
- Create: `backend/app/routers/system.py`
- Modify: `backend/app/main.py` (mount the router)
- Test: `backend/tests/test_resume_endpoint.py`

**Interfaces:**
- Consumes: `clear_safe_mode`, `run_recovery`, `get_gateway`.
- Produces: `APIRouter(prefix="/api/system")` with `POST /resume` (gated by `require_auth`):
  if `get_gateway()._ib is None` → return `{"conn_state": <state>, "safe_mode": <bool>, "detail":
  "not connected"}`; else `await clear_safe_mode()`, `await run_recovery(gw._ib, gw)`, return
  `{"conn_state": gw.state.value, "safe_mode": await is_safe_mode(), "safe_mode_reason": <reason>}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_resume_endpoint.py
"""resume() clears safe mode + recovers to READY on clean state.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_re.db python -m tests.test_resume_endpoint
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.safe_mode import is_safe_mode, trip_safe_mode
        from app.ibkr.gateway import ConnState, get_gateway
        from app.routers.system import resume

        await init_db()
        await trip_safe_mode("orphan AAPL")
        get_gateway().set_state(ConnState.SAFE_MODE)

        class CleanIB:
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []
        get_gateway()._ib = CleanIB()

        res = await resume()
        assert res["conn_state"] == "ready", res
        assert res["safe_mode"] is False and await is_safe_mode() is False
        print("PASS: resume cleared safe mode + recovered to READY")
    asyncio.run(_t())
    print("\nALL RESUME-ENDPOINT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_re.db ../.venv/bin/python -m tests.test_resume_endpoint`
Expected: FAIL — `ModuleNotFoundError: app.routers.system`.

- [ ] **Step 3: Implement**

```python
# backend/app/routers/system.py
"""System ops endpoints: resume from SAFE_MODE (clear flag + re-run recovery)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.gate import require_auth
from app.engine.recovery import run_recovery
from app.engine.safe_mode import clear_safe_mode, is_safe_mode
from app.ibkr.gateway import get_gateway

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/resume", dependencies=[Depends(require_auth)])
async def resume() -> dict:
    gw = get_gateway()
    if gw._ib is None:
        return {"conn_state": gw.state.value, "safe_mode": await is_safe_mode(),
                "detail": "not connected"}
    await clear_safe_mode()
    await run_recovery(gw._ib, gw)
    from app.db.base import session_scope
    from app.db.models import SystemFlag
    reason = None
    async with session_scope() as s:
        row = await s.get(SystemFlag, "safe_mode")
        if row and row.on:
            reason = row.reason or None
    return {"conn_state": gw.state.value, "safe_mode": await is_safe_mode(),
            "safe_mode_reason": reason}
```

In `app/main.py`, import and mount the router next to the other routers (match the existing
`app.include_router(...)` lines):

```python
    from app.routers.system import router as system_router
    app.include_router(system_router)
```

(Verify the exact mounting style in `main.py` — it may import routers at module top and call
`app.include_router(...)` after `app = FastAPI(...)`; follow that pattern.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_re.db ../.venv/bin/python -m tests.test_resume_endpoint && rm -f _t_re.db`
Expected: `ALL RESUME-ENDPOINT TESTS PASSED`.

**Regression:** `../.venv/bin/python -c "import app.main"` imports clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/system.py backend/app/main.py backend/tests/test_resume_endpoint.py
git commit -m "feat(api): POST /api/system/resume (clear safe mode + re-recover)"
```

---

### Task 5: Frontend — `api.resume` + connection-state badge

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/components/ApiTab.jsx`

**Interfaces:**
- Produces: `api.resume = () => request("/api/system/resume", { method: "POST" })`. The ApiTab badge
  text + class derive from `info.conn_state` (fallback to the `connected` bool when `conn_state` is
  absent).

- [ ] **Step 1: Add `api.resume`**

In `frontend/src/api.js`, next to `disconnect`, add:

```javascript
  resume: () => request("/api/system/resume", { method: "POST" }),
```

- [ ] **Step 2: Badge by `conn_state`**

In `ApiTab.jsx`, after `const connected = !!info?.connected;`, add a small mapping and use it for
the badge:

```javascript
  const state = info?.conn_state || (connected ? "connected" : "disconnected");
  const STATE_LABEL = { disconnected: "Disconnected", connecting: "Connecting…",
    connected: "Connected", recovering: "Recovering…", ready: "Ready", safe_mode: "SAFE MODE" };
  const STATE_CLASS = { ready: "live", connected: "live", recovering: "warn",
    connecting: "warn", safe_mode: "danger", disconnected: "disabled" };
```

Change the badge span to use them:

```jsx
          <span className={`mode-badge ${STATE_CLASS[state] || "disabled"}`}>
            {STATE_LABEL[state] || state}
          </span>
```

- [ ] **Step 3: Build to verify**

Run: `cd frontend && npm run build`
Expected: build succeeds (no syntax errors). Visually (optional `npm run dev`): the badge shows
`Ready` (green) when connected + recovered, `SAFE MODE` (red) when tripped.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.js frontend/src/components/ApiTab.jsx
git commit -m "feat(web): connection-state badge + api.resume"
```

---

### Task 6: Frontend — SAFE_MODE banner

**Files:**
- Create: `frontend/src/components/SafeModeBanner.jsx`
- Modify: `frontend/src/App.jsx` (render it app-level from `auth`)
- Modify: `frontend/src/styles.css` (banner style)

**Interfaces:**
- Consumes: `api.resume` (Task 5); `auth` (App's status state with `safe_mode`/`safe_mode_reason`).
- Produces: `SafeModeBanner({ auth, onResume })` — renders nothing unless `auth?.safe_mode`; else a
  red banner with the reason + a "Resume trading" button (confirm → `api.resume()` → `onResume()`
  refresh).

- [ ] **Step 1: Create the component**

```jsx
// frontend/src/components/SafeModeBanner.jsx
import { useState } from "react";
import { api } from "../api.js";

// App-level halt banner. Shown while the engine is in SAFE_MODE (an unreconcilable
// broker state). Resume clears safe mode and re-runs recovery server-side.
export default function SafeModeBanner({ auth, onResume }) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  if (!auth?.safe_mode) return null;

  const resume = async () => {
    setBusy(true);
    try {
      await api.resume();
      await onResume?.();
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="safemode-banner">
      <span className="safemode-text">
        ⚠ SAFE MODE — trading halted: {auth.safe_mode_reason || "unreconcilable broker state"}
      </span>
      {!confirming ? (
        <button className="btn danger" disabled={busy} onClick={() => setConfirming(true)}>
          Resume trading
        </button>
      ) : (
        <>
          <span>Re-check broker state and resume?</span>
          <button className="btn danger" disabled={busy} onClick={resume}>
            {busy ? "Resuming…" : "Confirm resume"}
          </button>
          <button className="btn ghost" disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Render it app-level**

In `App.jsx`, import it and render above the main content using the existing `auth` state and the
`refresh` callback:

```jsx
import SafeModeBanner from "./components/SafeModeBanner.jsx";
```

Place `<SafeModeBanner auth={auth} onResume={refresh} />` near the top of the returned layout
(above the tab content, so it spans the app).

- [ ] **Step 3: Add the banner style**

In `frontend/src/styles.css`, append:

```css
.safemode-banner {
  display: flex; align-items: center; gap: 12px;
  background: #4c1111; color: #ffd4d4; border-bottom: 1px solid #7a1d1d;
  padding: 10px 16px; font-weight: 600;
}
.safemode-text { flex: 1; }
```

- [ ] **Step 4: Build to verify**

Run: `cd frontend && npm run build`
Expected: build succeeds. Visually (optional): trip safe mode server-side (or stub) and confirm
the red banner shows with the reason and the Resume flow calls `/api/system/resume`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/SafeModeBanner.jsx frontend/src/App.jsx frontend/src/styles.css
git commit -m "feat(web): app-level SAFE_MODE banner with Resume"
```

---

## Manual smoke (after the suite is green)
Backend running + frontend built: trip safe mode (e.g. via a recovery against a paper account with
an unmatched position, or temporarily `trip_safe_mode` in a shell) → the badge turns red `SAFE MODE`
and the banner appears with the reason → click **Resume trading** → with a clean broker state it
clears to `Ready`; with the discrepancy still present it re-trips (banner persists).

## Self-review notes
- Spec §1 status fields → Task 1. §2 resume → Task 4. §3 alerting → Tasks 2–3. §4 frontend
  badge+banner → Tasks 5–6. §Testing → each task (backend unit; frontend manual build).
- Names consistent: `conn_state`/`safe_mode`/`safe_mode_reason`, `send_alert`/`fire_alert`/
  `register_channel`, `/api/system/resume`, `api.resume`, `SafeModeBanner`.
