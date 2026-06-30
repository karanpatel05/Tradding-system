# IBKR Always-Connect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The dashboard stays connected to IBKR — fixed ~5s reconnect, the nightly Gateway restart can't false-trip SAFE_MODE, no Disconnect button, auto-connect on load, and a "reconnecting" banner.

**Architecture:** Backend supervisor owns reconnection (works with no browser open); tune it to a fixed 5s retry and make recovery wait for account data to re-sync before reconciling. Frontend removes the Disconnect buttons, fires one connect on load when not ready, and shows an amber reconnecting banner.

**Tech Stack:** Python 3.14, asyncio, ib_async, FastAPI; React + Vite (no JS test runner).

**Source spec:** `docs/superpowers/specs/2026-06-30-ibkr-always-connect-design.md`

## Global Constraints

- Reconnection is **backend-driven** (the supervisor). The frontend never loops `connect()` — it
  fires at most one connect on page load and otherwise just reflects `conn_state`.
- Retry cadence is **fixed ~5s** (`ibkr_reconnect_interval_seconds`), not exponential.
- Recovery must **not** trip SAFE_MODE on the transient empty-positions window right after a Gateway
  restart; a *real* mismatch must still trip it.
- Keep `api.disconnect()` and `/api/auth/disconnect` (used by nothing user-facing) — only remove the
  buttons.
- Only one banner at a time: SAFE_MODE (red) takes precedence over the amber reconnecting banner.
- Backend tasks are TDD (module-run `_run()` tests via `DATABASE_URL=... python -m tests.<name>`).
  Frontend tasks: `npm run build` + visual (no JS test runner).

## File structure
- Modify `backend/app/config.py` — add `ibkr_reconnect_interval_seconds`, `ibkr_account_settle_seconds`.
- Modify `backend/app/ibkr/gateway.py` — `reconnect_loop` fixed interval; drop `backoff_schedule`.
- Modify `backend/app/engine/recovery.py` — `_await_account_ready` + call it before reconcile.
- Modify `backend/app/strategies` n/a. Tests: update `test_reconnect_backoff.py`, add `test_recovery_account_ready.py`.
- Modify `frontend/src/components/ApiTab.jsx`, `frontend/src/App.jsx` — remove Disconnect; auto-connect.
- Create `frontend/src/components/ConnectionBanner.jsx` + `frontend/src/styles.css` — amber banner.

---

### Task 1: Fixed-interval reconnect

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_reconnect_backoff.py` (rewrite → fixed interval)

**Interfaces:**
- Produces: config `ibkr_reconnect_interval_seconds: int = 5`. `Gateway.reconnect_loop(max_attempts=None)`
  retries `connect()` every `ibkr_reconnect_interval_seconds` (+≤0.5s jitter) until connected. The
  `backoff_schedule` staticmethod is removed.

- [ ] **Step 1: Rewrite the test (failing)**

Replace the body of `backend/tests/test_reconnect_backoff.py` with:

```python
"""Reconnect loop retries at a fixed interval until connected.
   Run: python -m tests.test_reconnect_backoff
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.config import get_settings
    from app.ibkr.gateway import Gateway

    assert not hasattr(Gateway, "backoff_schedule"), "exponential backoff_schedule should be gone"

    async def _loop_test():
        get_settings().ibkr_reconnect_interval_seconds = 0   # no real waiting in the test
        gw = Gateway()
        calls = {"n": 0}
        async def fake_connect():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("nope")
            return object()
        gw.connect = fake_connect          # type: ignore[assignment]
        await gw.reconnect_loop(max_attempts=5)
        assert calls["n"] == 3, calls
    asyncio.run(_loop_test())
    print("PASS: reconnect loop retries at fixed interval until connected")
    print("\nALL RECONNECT TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_reconnect_backoff`
Expected: FAIL — `assert not hasattr(Gateway, "backoff_schedule")` (it still exists), or
`AttributeError` on the new config field.

- [ ] **Step 3: Implement**

In `app/config.py`, after `ibkr_market_data_type` (line ~55), add:

```python
    ibkr_reconnect_interval_seconds: int = 5   # fixed reconnect retry cadence (always-connect)
    ibkr_account_settle_seconds: float = 2.0   # wait after reconnect before reconciling (data re-sync)
```

In `app/ibkr/gateway.py`, delete the `backoff_schedule` staticmethod and rewrite `reconnect_loop`:

```python
    async def reconnect_loop(self, max_attempts: int | None = None) -> None:
        interval = get_settings().ibkr_reconnect_interval_seconds
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            try:
                self.set_state(ConnState.CONNECTING)
                await self.connect()
                return
            except Exception:  # noqa: BLE001 — keep retrying through any connect failure
                delay = interval + random.uniform(0, 0.5)
                logger.warning("reconnect attempt %d failed; retrying in %.1fs", attempt, delay)
                await asyncio.sleep(delay)
                attempt += 1
                if attempt == 5:
                    from app.services.alerts import fire_alert
                    fire_alert("reconnect_failed",
                               f"reconnect still failing after {attempt} attempts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_reconnect_backoff`
Expected: `ALL RECONNECT TESTS PASSED`.

**Regression:** `cd backend && ../.venv/bin/python -m tests.test_heartbeat && ../.venv/bin/python -m tests.test_conn_events` still pass (gateway untouched elsewhere).

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/ibkr/gateway.py backend/tests/test_reconnect_backoff.py
git commit -m "feat(ibkr): fixed-interval (~5s) reconnect instead of exponential backoff"
```

---

### Task 2: Nightly-restart-safe recovery (await account readiness)

**Files:**
- Modify: `backend/app/engine/recovery.py`
- Test: `backend/tests/test_recovery_account_ready.py`

**Interfaces:**
- Consumes: `ibkr_account_settle_seconds` (Task 1).
- Produces: `async _await_account_ready(ib) -> None` — polls `ib.managedAccounts()` until non-empty
  (≤10s), then sleeps `ibkr_account_settle_seconds`. `run_recovery` calls `await _await_account_ready(ib)`
  right after `set_state(RECOVERING)` and before `fetch_snapshot`, so a just-reconnected Gateway whose
  positions haven't re-synced doesn't produce a false mismatch.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_recovery_account_ready.py
"""Recovery waits for account data to re-sync before reconciling, so a just-restarted
Gateway (positions briefly empty) does NOT false-trip SAFE_MODE.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_ar.db python -m tests.test_recovery_account_ready
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine import recovery
        from app.ibkr.gateway import ConnState, Gateway

        get_settings().ibkr_account_settle_seconds = 0   # no real settle wait in the test
        await init_db()
        async with session_scope() as s:
            s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                              quantity=10, status="open"))

        class C:
            def __init__(self, sym): self.symbol = sym; self.secType = "STK"
        class Pos:
            def __init__(self, sym, q): self.contract = C(sym); self.position = q; self.avgCost = 100.0

        class RestartingIB:
            """Accounts + positions are empty until 'synced' (2nd managedAccounts poll)."""
            def __init__(self): self.polls = 0
            def managedAccounts(self):
                self.polls += 1
                return ["DU1"] if self.polls >= 2 else []
            async def reqPositionsAsync(self):
                return [Pos("AAPL", 10)] if self.polls >= 2 else []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        ok = await recovery.run_recovery(RestartingIB(), gw)
        assert ok is True and gw.state is ConnState.READY, gw.state
        print("PASS: recovery waited for account sync, reconciled to READY")
    asyncio.run(_t())
    print("\nALL RECOVERY-ACCOUNT-READY TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_ar.db ../.venv/bin/python -m tests.test_recovery_account_ready`
Expected: FAIL — recovery reconciles immediately against empty positions → mismatch → `SAFE_MODE`
(`assert ... gw.state is READY` fails, state is `safe_mode`).

- [ ] **Step 3: Implement**

In `app/engine/recovery.py`, add the helper and call it at the top of `run_recovery`:

```python
async def _await_account_ready(ib) -> None:
    """Wait for a (re)connected Gateway's account data to re-sync before reconciling, so a
    nightly-restart's brief empty-positions window doesn't look like a real mismatch."""
    import asyncio

    from app.config import get_settings
    waited = 0.0
    while waited < 10.0:
        try:
            # A stub IB without managedAccounts is treated as "ready" (don't wait 10s in tests).
            accts = ib.managedAccounts() if hasattr(ib, "managedAccounts") else ["_"]
            if accts:
                break
        except Exception:  # noqa: BLE001 — not ready yet
            pass
        await asyncio.sleep(0.2)
        waited += 0.2
    settle = get_settings().ibkr_account_settle_seconds
    if settle:
        await asyncio.sleep(settle)
```

In `run_recovery`, change the start from:

```python
    gateway.set_state(ConnState.RECOVERING)
    snap = await fetch_snapshot(ib)
```

to:

```python
    gateway.set_state(ConnState.RECOVERING)
    await _await_account_ready(ib)
    snap = await fetch_snapshot(ib)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_ar.db ../.venv/bin/python -m tests.test_recovery_account_ready && rm -f _t_ar.db`
Expected: `ALL RECOVERY-ACCOUNT-READY TESTS PASSED`.

**Regression:** `test_recovery_sequence`, `test_recovery_backfill`, `test_alert_wiring` still pass.
Their stub IBs have **no** `managedAccounts`, so the `hasattr` guard in Step 3 treats them as ready
(no 10s wait) and `ibkr_account_settle_seconds` defaults to 2.0 — to keep these fast, add
`get_settings().ibkr_account_settle_seconds = 0` at the top of each of those three tests' `_run()`.
Re-run all three (each with a throwaway `DATABASE_URL`) and confirm they pass quickly.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/recovery.py backend/tests/test_recovery_account_ready.py \
  backend/tests/test_recovery_sequence.py backend/tests/test_recovery_backfill.py backend/tests/test_alert_wiring.py
git commit -m "feat(engine): recovery waits for account re-sync (no false SAFE_MODE on nightly restart)"
```

---

### Task 3: Remove the Disconnect buttons

**Files:**
- Modify: `frontend/src/components/ApiTab.jsx`
- Modify: `frontend/src/App.jsx`

**Interfaces:**
- Produces: no user-facing Disconnect control. `api.disconnect()` stays in `api.js` (unused by UI).

- [ ] **Step 1: ApiTab — remove the Disconnect button + handler**

In `ApiTab.jsx`, delete the `disconnect` async handler and the JSX button that calls it (the
`{connected && (<button ... onClick={disconnect}>…Disconnect…</button>)}` block). Keep the Connect
button for the disconnected state. Update the component's top comment to drop "Disconnect".

- [ ] **Step 2: App sidebar — remove Disconnect, keep Connect fallback**

In `App.jsx`, replace the sidebar-foot button block:

```jsx
          {auth.connected ? (
            <button className="btn ghost full" onClick={disconnect}>Disconnect</button>
          ) : (
            <button className="btn ghost full" disabled={connecting} onClick={connect}>
              {connecting ? "Connecting…" : "Connect IBKR"}
            </button>
          )}
```

with (only a Connect fallback while disconnected; nothing while connected):

```jsx
          {!auth.connected && (
            <button className="btn ghost full" disabled={connecting} onClick={connect}>
              {connecting ? "Connecting…" : "Connect IBKR"}
            </button>
          )}
```

Delete the now-unused `disconnect` handler in `App.jsx`.

- [ ] **Step 3: Build to verify**

Run: `cd frontend && npm run build`
Expected: build succeeds; no references to `disconnect` remain in `App.jsx`/`ApiTab.jsx`
(`grep -n "disconnect" frontend/src/App.jsx frontend/src/components/ApiTab.jsx` → only none, or
unrelated). Visually: no Disconnect button anywhere.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ApiTab.jsx frontend/src/App.jsx
git commit -m "feat(web): remove Disconnect button (always-connect model)"
```

---

### Task 4: Auto-connect on load + reconnecting banner

**Files:**
- Create: `frontend/src/components/ConnectionBanner.jsx`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `auth` (App status state with `conn_state`/`connected`/`safe_mode`), `api.connect`.
- Produces: `ConnectionBanner({ auth })` — renders an amber banner when
  `auth.conn_state ∈ {disconnected, connecting, recovering}` AND `!auth.safe_mode`; nothing
  otherwise. App fires `api.connect()` once on load when not connected.

- [ ] **Step 1: Create the banner**

```jsx
// frontend/src/components/ConnectionBanner.jsx
// Amber app-level banner shown while the IBKR connection is not READY (and not SAFE_MODE,
// which has its own red banner). The backend supervisor is reconnecting in the background.
const RECONNECTING = new Set(["disconnected", "connecting", "recovering"]);

export default function ConnectionBanner({ auth }) {
  if (!auth || auth.safe_mode) return null;            // SAFE_MODE banner takes precedence
  const state = auth.conn_state || (auth.connected ? "connected" : "disconnected");
  if (!RECONNECTING.has(state)) return null;
  const msg = state === "recovering"
    ? "Reconnected — recovering…"
    : "IBKR disconnected — reconnecting…";
  return <div className="reconnect-banner">⚡ {msg}</div>;
}
```

- [ ] **Step 2: Render it + auto-connect on load (App.jsx)**

Import it next to `SafeModeBanner`:

```jsx
import ConnectionBanner from "./components/ConnectionBanner.jsx";
```

Render it directly under `SafeModeBanner` (inside the existing top fragment):

```jsx
      <SafeModeBanner auth={auth} onResume={refresh} />
      <ConnectionBanner auth={auth} />
```

Add a one-shot auto-connect effect (after the existing status `useEffect`), using a ref so it fires
once per page load when not connected:

```jsx
  const kickedRef = useRef(false);
  useEffect(() => {
    if (kickedRef.current || auth === null) return;
    const ready = auth.conn_state === "ready" || auth.conn_state === "connected" || auth.connected;
    if (!ready) {
      kickedRef.current = true;
      api.connect().catch(() => {}).finally(refresh);  // backend supervisor keeps retrying after this
    }
  }, [auth, refresh]);
```

Add `useRef` to the React import at the top of `App.jsx`:

```jsx
import { useCallback, useEffect, useRef, useState } from "react";
```

- [ ] **Step 3: Banner style**

Append to `frontend/src/styles.css`:

```css
.reconnect-banner {
  display: flex; align-items: center; gap: 10px;
  background: #4a3a10; color: #ffe9b0; border-bottom: 1px solid #7a5d1d;
  padding: 8px 16px; font-weight: 600;
}
```

- [ ] **Step 4: Build to verify**

Run: `cd frontend && npm run build`
Expected: build succeeds. Visually: with the Gateway down, the amber "IBKR disconnected —
reconnecting…" banner shows and clears once `conn_state` is `ready`; loading the page while
disconnected fires one connect attempt.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ConnectionBanner.jsx frontend/src/App.jsx frontend/src/styles.css
git commit -m "feat(web): auto-connect on load + amber reconnecting banner"
```

---

## Manual smoke (after the suite is green)
With the server running and connected (badge READY): stop the IB Gateway. Within ~5s the backend
logs reconnect attempts, the badge goes amber and the "reconnecting…" banner appears. Restart the
Gateway → it reconnects, recovery waits for account sync, reaches READY, and the banner clears — no
SAFE_MODE. This mirrors the nightly 11:45 PM restart.

## Self-review notes
- Spec §1 fixed reconnect → Task 1. §2 account-ready recovery → Task 2. §3 remove Disconnect →
  Task 3. §4 auto-connect → Task 4. §5 banner → Task 4. Testing → each task.
- Names consistent: `ibkr_reconnect_interval_seconds`, `ibkr_account_settle_seconds`,
  `_await_account_ready`, `ConnectionBanner`, `conn_state` values.
