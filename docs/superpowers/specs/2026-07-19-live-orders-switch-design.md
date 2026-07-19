# Live-Orders Switch — Design

**Date:** 2026-07-19
**Status:** Approved

## Goal

A UI switch to flip the `ENABLE_LIVE_ORDERS` safety gate at runtime — no server
restart, no manual `.env` editing. OFF = paper (orders logged as no-ops, the
current default). ON = real orders transmitted to Tastytrade.

## Decisions (user-approved)

1. **Placement:** API tab (next to broker credentials). Dashboard gets a
   read-only PAPER/LIVE badge only.
2. **Safety UX:** turning ON requires typing `LIVE` to confirm. Turning OFF is
   always instant, one click.
3. **Persistence:** state is written to `backend/.env`
   (`ENABLE_LIVE_ORDERS=true|false`) so it survives restarts — same pattern as
   broker credentials.

## Architecture

Mirror the existing credentials pattern in `app/auth/routes.py` (`tt_save`):
mutate the `lru_cache`d `Settings` object in place **and** persist with
`_upsert_env`. All ~10 order-gate checks call
`get_settings().enable_live_orders` at call time, so the in-place mutation
takes effect immediately.

Rejected alternative: a DB `SystemFlag` row (like `safe_mode`) — creates a
second source of truth next to the env var and would require rewiring every
gate check.

## Backend

New endpoint in `backend/app/routers/system.py`:

- `PUT /api/system/live-orders` — body `{"enabled": bool}`, `require_auth`.
  - Sets `get_settings().enable_live_orders = enabled`.
  - Persists `ENABLE_LIVE_ORDERS` to `backend/.env` via the `_upsert_env`
    helper (shared from `app/auth/routes.py`).
  - Logs a prominent `[LIVE-ORDERS]` warning on every flip (old → new state).
  - Returns `{"live_orders_enabled": <bool>}`.

No new GET: `/health` already reports `live_orders_enabled`.

The confirmation ("type LIVE") is frontend-only friction; the API itself takes
a plain boolean. Rationale: the API is already auth-gated, and server-side
confirmation tokens add state for no real safety gain in a single-user app.

## Frontend

- **ApiTab** (`frontend/src/components/ApiTab.jsx`): new "Order routing" card.
  - Shows current mode: PAPER (muted) / LIVE (red accent).
  - OFF → ON: click reveals an inline confirm row — text input, arm button
    enabled only when the input equals `LIVE`; on confirm, `PUT` with
    `{enabled: true}`.
  - ON → OFF: single click, immediate `PUT {enabled: false}`.
  - Errors render inline, matching the credentials form.
- **Dashboard** (`frontend/src/components/Dashboard.jsx`): small read-only
  badge — "PAPER" in the muted style, "LIVE" in red — driven by status data the
  dashboard already polls (falls back to `/health` if needed).
- `frontend/src/api.js`: add `setLiveOrders(enabled)`.

## Interaction with existing safety layers

Unchanged. SAFE_MODE still blocks execution regardless of this switch; the
kill switch still flattens positions. This switch only controls whether orders
are transmitted vs. logged as paper no-ops.

## Error handling

- Invalid body → FastAPI 422.
- Unauthenticated → existing 401 from `require_auth`.
- `.env` write failure → 500; the in-memory flag is NOT left flipped (set it
  only after the file write succeeds, so UI state and persisted state agree).

## Testing

- Backend (pytest, TDD): endpoint flips the cached setting; writes
  `ENABLE_LIVE_ORDERS` to `.env`; requires auth; returns the new state;
  turning OFF works with a plain `{"enabled": false}`; order-submission gate
  in `app/broker/orders.py` follows the flipped value.
- Frontend: drive the UI (toggle on with confirm, toggle off) against the
  running backend.
