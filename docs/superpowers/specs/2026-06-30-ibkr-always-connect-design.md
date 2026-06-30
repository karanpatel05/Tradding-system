# IBKR Always-Connect (auto-reconnect UX + nightly-restart-safe recovery) — Design

> **Status:** approved design (2026-06-30). Builds on the resilience work (Phase 1/2a/2b on
> `main`). Backend tuning + a frontend UX change.

## Problem

The operator wants the dashboard to "just stay connected" to the IBKR Gateway:
1. **No Disconnect button** — there should be no manual way to disconnect; the system is
   always-connect.
2. **Auto-connect on load** — opening the website should trigger a connection attempt if not
   already connected.
3. **Alert + keep retrying** — when not connected, show an alert and keep trying to connect every
   few seconds.
4. **Nightly Gateway restart** — the IB Gateway restarts ~11:45 PM (its daily maintenance),
   dropping the connection for ~1–2 minutes; the system must ride through it cleanly.

Today: the backend already auto-reconnects (Phase 1 supervisor: detect disconnect → reconnect →
recovery), but with exponential backoff (1→60s, can wait ~60s between late attempts). The frontend
has **Disconnect** buttons (ApiTab + sidebar), does not auto-connect on load, and has no
"reconnecting" indicator. And a subtle hazard: right after the nightly restart, the broker's
positions/executions can be momentarily empty before the account data farm re-syncs, which could
make recovery see a "broker flat vs local open" mismatch and **wrongly trip SAFE_MODE** — halting
trading overnight.

## Decisions (locked)
- **Retry cadence:** fixed **~5s** while disconnected (not exponential).
- **Nightly restart:** **generic auto-reconnect** — no special maintenance window; a brief
  "reconnecting" alert appears each night and clears when reconnected.
- **Reconnection owner:** the **backend supervisor** (works even with no browser open); the
  frontend reflects state, kicks an initial connect on load, and shows the alert.

## Components

### 1. Fixed-interval reconnect — `app/ibkr/gateway.py` + `app/config.py`
- Add config `ibkr_reconnect_interval_seconds: int = 5`.
- `Gateway.reconnect_loop` retries `connect()` every `ibkr_reconnect_interval_seconds` (fixed) until
  connected (or `max_attempts` if given), instead of `backoff_schedule`'s exponential 1→60s. Keep a
  small jitter (≤0.5s) to avoid thundering. `backoff_schedule` is removed (only `reconnect_loop`
  used it) or left unused; the test that asserts the schedule is updated/removed accordingly.
- The supervisor already calls `reconnect_loop()` on `DISCONNECTED`; no change there beyond the
  faster cadence.

### 2. Nightly-restart-safe recovery — `app/engine/recovery.py`
- Before reconciling, `run_recovery` **waits for account readiness** so a just-reconnected Gateway
  whose account data hasn't re-synced doesn't produce a false mismatch:
  ```
  async def _await_account_ready(ib, timeout=10.0, settle=2.0) -> None:
      # poll ib.managedAccounts() until non-empty (or timeout), then a short settle
      # so reqPositions/reqExecutions reflect the real account, not an empty pre-sync state.
  ```
  `run_recovery` calls `await _await_account_ready(ib)` right after setting `RECOVERING` and before
  `fetch_snapshot`. This is best-effort: on timeout it proceeds (recovery still fails safe — a real
  mismatch still trips SAFE_MODE; only the *transient empty* window is absorbed).
- Rationale: turns the nightly restart into a clean `DISCONNECTED → reconnect → READY` cycle instead
  of a spurious SAFE_MODE halt. Applies to every recovery (cold boot included), which is correct.

### 3. Remove Disconnect from the UI — `frontend/src/components/ApiTab.jsx`, `frontend/src/App.jsx`
- Delete the **Disconnect** button + handler from `ApiTab` and the **Disconnect** button in the
  `App` sidebar foot. The `/api/auth/disconnect` endpoint and `api.disconnect()` remain (used by
  nothing user-facing) — only the buttons are removed. The system is always-connect.

### 4. Auto-connect on load — `frontend/src/App.jsx`
- On first mount, after the initial status load, if `conn_state` is not `ready`/`connected`, fire
  one `api.connect()` to kick an immediate attempt (don't wait for the supervisor tick). Guard with
  a ref so it fires once per page load, not every poll. The ongoing retry is the backend's job —
  the frontend does **not** loop `connect()`.

### 5. "Reconnecting" alert banner — `frontend/src/components/ConnectionBanner.jsx` (new) + `App.jsx`
- New app-level amber banner shown while `auth.conn_state ∈ {disconnected, connecting, recovering}`
  **and** not `safe_mode` (SAFE_MODE has its own red banner, which takes precedence):
  *"⚡ IBKR disconnected — reconnecting…"* (or "Recovering…" for the `recovering` state).
- Reads `auth` (already polled every 5s in `App`); auto-clears when `conn_state` is `ready`. Styled
  like `SafeModeBanner` but amber (`#4a3a10` bg). Rendered alongside `SafeModeBanner` at app level.

## Data flow
```
Gateway restart 11:45pm → disconnectedEvent → state DISCONNECTED
        │ supervisor: reconnect_loop (every 5s) until connected
        ▼
   connected → RECOVERING → _await_account_ready → reconcile → READY
        │ (no false SAFE_MODE during the empty-data window)
        ▼ /api/auth/status conn_state
   frontend poll (5s): amber "reconnecting" banner while not ready → clears at READY
   (on page load: if not ready, one api.connect() kick)
```

## Error handling
- `_await_account_ready` timeout → proceed with reconcile (don't block recovery forever); a real
  discrepancy still trips SAFE_MODE.
- Frontend auto-connect kick failure → ignored (backend supervisor keeps retrying); the banner stays
  until `ready`.
- Reconnect every 5s against a down Gateway is acceptable for a local Gateway; the existing
  `reconnect_failed` alert still fires once at the 5th failed attempt (~25s), consistent with the
  "generic auto-reconnect" choice (a brief nightly alert).

## Testing
- **Backend `reconnect_loop`** (`test_reconnect_backoff` updated → `test_reconnect_interval`): with a
  stubbed `connect` failing twice then succeeding and the interval monkeypatched to 0, it retries at
  the fixed cadence and connects (no exponential growth).
- **Recovery readiness** (`test_recovery_account_ready`): an IB stub whose `reqPositionsAsync`
  returns `[]` on the first call then the real position, with a local open trade → recovery waits
  and reconciles against the populated positions → READY (would have tripped SAFE_MODE without the
  wait). Use a tiny timeout/settle in the test.
- **Existing** `test_recovery_sequence` / `test_alert_wiring` still pass (real mismatch still trips
  SAFE_MODE).
- **Frontend** (no JS test runner): `npm run build` succeeds; visual — no Disconnect button; amber
  "reconnecting" banner shows for a non-ready `conn_state` and clears at `ready`; one connect fires
  on load when disconnected.

## Out of scope
A configurable maintenance window / quiet-hours alert suppression (user chose generic
auto-reconnect). Frontend-driven retry loops (backend owns retry). Changing the Gateway's own
restart time (an IBKR Gateway setting, not app code).

## Implementation notes
- Keep `api.disconnect()` and `/api/auth/disconnect` (no dead-code removal) — only the buttons go.
- `_await_account_ready` polls with `asyncio.sleep`; keep total bounded (≤ ~10s) so startup isn't
  noticeably slower when the account is already ready (it returns as soon as accounts are non-empty).
- **Only one banner shows at a time.** SAFE_MODE (red, halt) takes precedence: when
  `auth.safe_mode` is true, render `SafeModeBanner` and suppress the amber `ConnectionBanner`. The
  amber banner shows only when `not safe_mode` and `conn_state` is non-ready.
