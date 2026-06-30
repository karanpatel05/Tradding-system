# IBKR Resilience — Phase 2b (Ops Visibility) Design

> **Status:** approved design (2026-06-29). Surfaces Phase 1/2a state to the operator and adds
> alerting. Builds on Phase 1 (`2026-06-28-ibkr-resilience-design.md`) + Phase 2a
> (`2026-06-29-ibkr-resilience-phase2a-design.md`), both on `main`.

## Problem

The resilience engine works, but the operator is blind to it:
- `/api/auth/status` exposes only a `connected` boolean — no `ConnState`
  (`RECOVERING`/`READY`/`SAFE_MODE`), so the UI cannot show that trading is paused or halted.
- There is **no way to clear SAFE_MODE** from the UI; once tripped, the system stays halted with
  no operator path to review-and-resume.
- There is **no alerting**: a disconnect, a SAFE_MODE trip, or a prolonged reconnect failure
  produces only log lines / scattered journal events — nothing that surfaces prominently.

## Goal

Make the resilience state visible and actionable: a connection-state badge, a prominent SAFE_MODE
banner with the discrepancy reason and a review-and-**Resume** action, and in-app alerts on the
three key events. Alerting is **pluggable** but ships **in-app only** (journal + UI); webhook/email
can be added later without touching call sites.

## Decisions (locked)
- **Alert channel:** in-app only (journal event + structured log), pluggable for later.
- **UI depth:** connection-state badge + SAFE_MODE banner + Resume button. Recovery/alert detail
  stays in the existing journal/log view (not a dedicated panel).
- **Resume semantics:** Resume clears the SAFE_MODE flag then **re-runs recovery** — it reaches
  READY only if the broker state now reconciles; otherwise it re-trips SAFE_MODE. Resume never
  blindly forces READY.

## Approach (chosen: A — extend existing status + small alerts module + minimal UI)

Fold the resilience fields into the status payload the frontend already polls, add one resume
endpoint, add a pluggable `alerts` service (default = journal + log), and add the badge + banner
to the current React app. Rejected: **B** (dedicated `/api/system/status` + second poll — extra
surface for marginal purity); **C** (WebSocket push — more complexity; the dashboard already polls).

## Components

### 1. Status exposure — `app/auth/routes.py` (`ConnectionStatus` + `_status`)
`ConnectionStatus` gains three fields:
- `conn_state: str` — `get_gateway().state.value` (`disconnected|connecting|connected|recovering|ready|safe_mode`).
- `safe_mode: bool` — `await is_safe_mode()`.
- `safe_mode_reason: str | None` — the `SystemFlag(key="safe_mode").reason` when on, else `None`.

`_status()` reads these (the safe-mode read is a cheap DB lookup; on error it defaults to
`safe_mode=False`, `conn_state` still reflects the gateway). The endpoint stays ungated (the
frontend polls it for the badge).

### 2. Resume endpoint — `app/routers/system.py` (new)
`POST /api/system/resume` (gated by `require_auth`): calls `clear_safe_mode()` then
`run_recovery(get_gateway()._ib, get_gateway())`; returns `{conn_state, safe_mode,
safe_mode_reason}` (the post-recovery state). If the Gateway isn't connected, returns a 409-style
detail rather than crashing. Mounted under `/api/system`.

### 3. Alerting — `app/services/alerts.py` (new)
```
AlertChannel = Callable[[str, str, dict | None], Awaitable[None]]
def register_channel(ch: AlertChannel) -> None
async def send_alert(kind: str, message: str, data: dict | None = None) -> None
```
- `kind` ∈ `{"disconnect", "safe_mode", "reconnect_failed"}`.
- `send_alert` dispatches to every registered channel; a channel raising is logged and skipped
  (one bad channel never blocks the others).
- The **default channel** (registered at import) writes a `system`-category journal event via
  `log_event(message, category="system", level="warning"|"error", data=...)` and a structured
  `logger` line. Webhook/email are future channels appended via `register_channel`.

Wiring (call `send_alert` from existing code):
- `Gateway._on_disconnect` / `_on_error(1100)` → `send_alert("disconnect", ...)`.
- `recovery.run_recovery` on the SAFE_MODE branch → `send_alert("safe_mode", reason, {...})`
  (replaces/augments the existing `log_event`).
- `Gateway.reconnect_loop` after a threshold of failed attempts (e.g. ≥ 5) → one
  `send_alert("reconnect_failed", ...)` (not per-attempt — avoid spam).

### 4. Frontend — connection-state badge + SAFE_MODE banner
- **Badge** (replaces the bool connection badge): colour by `conn_state` — `disconnected` gray,
  `connecting`/`recovering` amber, `connected` blue, `ready` green, `safe_mode` red. Reads
  `authStatus().conn_state`. Existing poll cadence; no new fetch.
- **SAFE_MODE banner** (`components/SafeModeBanner.jsx`, app-level): shown while
  `authStatus().safe_mode` is true. Displays `safe_mode_reason` and a **"Resume trading"** button
  → `api.resume()` (KillSwitch-style: confirm → call → show result). On a READY result the banner
  clears on the next status poll. `api.resume = () => request("/api/system/resume", {method:"POST"})`.

## Data flow
```
disconnect / safe-mode / prolonged reconnect
        │ send_alert(kind, msg, data)
        ▼
   default channel → journal (system event) + log   ──► journal/log view
        │
gateway/recovery set ConnState ──► /api/auth/status (conn_state, safe_mode, reason)
        │ frontend poll
        ▼
   badge (colour by state) + SAFE_MODE banner (reason + Resume)
        │ Resume click → POST /api/system/resume → clear_safe_mode + run_recovery
        ▼
   READY (banner clears) or SAFE_MODE again (reason persists)
```

## Error handling
- `_status` safe-mode read failure → treat as `safe_mode=False` (don't break the badge); log.
- Resume when Gateway disconnected → return a detail, no crash; state stays as-is.
- `send_alert` channel exceptions are caught per-channel; alerting never raises into the caller
  (a failed alert must not break a disconnect handler or recovery).

## Testing
- **Status payload** (`test_status_resilience_fields`): with gateway state set + safe-mode tripped,
  `_status()` returns `conn_state`, `safe_mode=True`, the reason.
- **Resume** (`test_resume_endpoint`): safe-mode on + a stubbed clean IB → resume clears safe mode
  and recovers to READY; with an orphan position → resume re-trips SAFE_MODE (reason persists).
- **Alerts** (`test_alerts`): a registered stub channel receives `send_alert(...)`; the default
  channel writes a `system` journal event; a channel that raises is skipped without propagating.
- **Frontend**: the project has **no JS test runner**, so the badge + banner are verified
  **manually** — `npm run build` succeeds, and a quick visual check that the badge colours by
  `conn_state` and the SAFE_MODE banner renders with the reason + working Resume button. No
  automated frontend test is added (consistent with the existing frontend, which has none).

## Out of scope
External alert channels (webhook/email) — pluggable hooks ship but no channel is wired now.
A dedicated recovery-report/alerts panel (detail stays in the journal). Auto-applying confirmed
missed close-fills (a reconciliation feature, tracked separately).

## Implementation notes
- Keep `/api/auth/status` ungated (frontend polls it pre-auth for the badge).
- `run_recovery` already sets state + schedules backfill; resume reuses it unchanged.
- Register the default alert channel once at module import of `alerts.py`; wiring sites call
  `send_alert` (fire-and-forget within the caller's async context).
