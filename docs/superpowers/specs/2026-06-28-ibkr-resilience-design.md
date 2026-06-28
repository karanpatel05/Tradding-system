# IBKR Resilience & Recovery — Design

> **Status:** approved design (2026-06-28). Covers the *whole* professional resilience
> surface; the implementation **plan** will phase it (see Phasing).

## Problem

The live engine trades against a single IB Gateway connection. Today that connection is
**passive and stateless about failure**:

- `Gateway.get_ib()` only reconnects lazily on the next call — there is no disconnect
  *detection*, no heartbeat, no auto-reconnect/backoff.
- The worker's live `State` (managed symbol, armed entry, stop/target, open position) is
  **in-memory only** — wiped on restart. A restarted worker comes up **flat even if a real
  position is open at IBKR**.
- The account layer can read positions/summary but has **no order/fill/execution
  reconciliation** — fills that happen while disconnected are never learned.
- The history cache has TTL + stale-fallback but **no gap backfill** after a disconnect.

Net effect: a dropped connection or a server reboot can leave the system's view of the world
**silently wrong** — thinking it is flat while holding, double-submitting an order across a
reconnect, or acting on a hole in the price series.

## Goal

Bring the server up to the resilience a professional trading server maintains, for **both**
the equity strategies and the Triple Calendar options strategy:

1. Detect disconnects and auto-reconnect.
2. On every (re)connect, **reconcile against broker truth** (orders, fills, positions).
3. Never double-submit an order across a reconnect (**idempotent orders**).
4. **Backfill** the market data missed during the gap.
5. **Persist and rebuild** the worker's working state so a restart resumes mid-trade.
6. Run a **deterministic startup/recovery sequence**.
7. On anything that cannot be reconciled, **HALT into safe mode and alert** — never trade on
   an ambiguous picture.

## Defining decision — safety posture

**On any unreconcilable broker state (an open position with no local record, a fill that
cannot be matched, a qty/partial mismatch): trip SAFE MODE — stop all order submission, raise
an alert, and require explicit human confirmation before resuming.** This is the institutional
default and anchors the entire recovery sequence. (Alternatives — auto-adopt broker positions,
or auto-flatten unknowns — were rejected.)

## Approach (chosen: A — central connection state machine + recovery coordinator)

A single **ConnectionManager** owns the IB connection lifecycle and publishes connection
state. A separate **RecoveryCoordinator** runs one deterministic recovery sequence on every
transition into "connected." Workers become *dumb*: they subscribe to connection state and
pause while disconnected/recovering. Reconciliation, backfill, durable state, and idempotency
are discrete, independently testable units.

Rejected: **B (per-worker self-healing)** — duplicated logic and multiple workers racing to
reconcile one shared account, no global safe-mode. **C (full event-sourced ledger + external
watchdog)** — over-engineered for this scale; we borrow its append-only-journal idea (already
present) but not the machinery.

## Components

### 1. ConnectionManager (upgrades `app/ibkr/gateway.py`)
- **State machine:** `DISCONNECTED → CONNECTING → CONNECTED → RECOVERING → READY`, plus
  `SAFE_MODE`. Trading is permitted **only** in `READY`.
- **Event subscriptions (ib_async):** `disconnectedEvent`, `connectedEvent`, `errorEvent`.
  The IBKR system-message **error codes are the canonical signals**:
  - `1100` connectivity lost → go DISCONNECTED, pause workers.
  - `1101` restored, **market data lost** → reconnect path MUST re-subscribe + backfill.
  - `1102` restored, data maintained → lighter recovery (still reconcile orders/fills).
  - market-data farm `2103/2105/2157` (down) vs `2104/2106/2158` (ok) — track per farm.
- **Heartbeat watchdog:** periodic `reqCurrentTimeAsync()` with a timeout; a miss ⇒ treat the
  socket as dead and force a reconnect (covers half-open sockets the OS hasn't dropped).
- **Auto-reconnect:** exponential backoff with jitter (1s → 2s → … → cap 60s), retry
  indefinitely until connected or manually stopped. One client per `clientId` (a duplicate
  clientId hangs — known gotcha).
- **Publishes** connection-state transitions to subscribers (workers, RecoveryCoordinator, UI).

### 2. ReconciliationService (`app/engine/reconciliation.py`, new)
Runs on every transition into CONNECTED, **before** trading resumes:
- **Sources:** `reqAllOpenOrdersAsync()` (working orders), `reqExecutionsAsync(ExecutionFilter(
  time=last_known))` + live `execDetailsEvent`/`commissionReportEvent` (fills since we went
  blind), `reqPositionsAsync()` (actual net positions).
- **Matching keys:** `orderRef` (our client order ref) → `permId` → `execId`.
- **Reconciliation matrix (per symbol / conId):**
  | local \ broker | flat | matching position | unknown position |
  |---|---|---|---|
  | flat | OK | **SAFE_MODE** | **SAFE_MODE** |
  | open (matches) | apply missed close-fill if a known order closed it; else **SAFE_MODE** | OK (adopt avg price if needed) | **SAFE_MODE** |
  | qty / partial mismatch | **SAFE_MODE** | **SAFE_MODE** | **SAFE_MODE** |
- Applies **confirmed** missed fills to `TradeRecord` / `OptionPositionRecord`.
- **Output:** `clean → resume` OR `trip safe-mode + structured discrepancy report` (journaled).

### 3. Idempotent order submission (extends `app/engine/execution.py` + order store)
- Before transmit: persist the order intent with a unique **`client_order_ref`**
  (e.g. `{strategy}-{symbol}-{uuid8}`), status `submitting`; set `order.orderRef = ref`.
- On acceptance, store IBKR's **`permId`** back on the record as the durable broker id; status
  → `working`/`filled`.
- Reconnect reconciliation dedupes by `orderRef`/`permId`, so an order submitted-but-unacked
  before a disconnect is **never re-sent**. An intent stuck in `submitting` with no broker
  order/fill is resolved by query; if still ambiguous → SAFE_MODE.

### 4. BackfillService (`app/services/backfill.py`, new)
- On reconnect (mandatory after `1101`; also whenever gap detected): for each symbol a strategy
  is actively using, fetch historical bars over `[last_cached_bar_time, now]` via the existing
  `IBKRHistoricalProvider` + `history_cache`.
- **Pacing-aware:** serialize requests, respect IBKR's ~60 requests / 10 min; backoff on
  pacing violations.
- **Merge/dedupe** by bar timestamp; never overwrite a good bar with a partial; mark the gap
  filled. **Re-subscribe** live `reqMktData` streams dropped on `1101`.
- **Gap gate:** a strategy does not act until its series is contiguous up to the latest closed
  bar.

### 5. Durable strategy state (`StrategyState` extension or new `WorkerState` table)
- Persist the worker's working memory: managed symbol, armed entry (side/price/stop/target/
  expiry-bar), open-trade ref (FK to `TradeRecord`/`OptionPositionRecord`), last-acted bar time.
- Written on **every** state transition (arm / enter / exit / cancel) — small and cheap.
- On startup, the RecoveryCoordinator rebuilds the in-memory `State` from this snapshot, then
  **cross-checks it against reconciled broker truth**: snapshot says "long AAPL x10 via trade
  #42" + broker confirms the position ⇒ resume mid-trade; broker disagrees ⇒ SAFE_MODE.

### 6. RecoveryCoordinator (`app/engine/recovery.py`, new)
Deterministic order, identical for cold start and reconnect:
1. Connect (ConnectionManager).
2. State `RECOVERING`; ensure all workers paused.
3. Reconcile: open orders → fills → positions (ReconciliationService).
4. **Unreconciled? → `SAFE_MODE` + alert; stop here (no trading).**
5. Backfill market data for active symbols + re-subscribe live streams (BackfillService).
6. Rebuild durable strategy state; cross-check vs reconciled positions.
7. Consistent ⇒ state `READY`; resume/unpause workers.
8. Write a **recovery report** to the journal: what was found/fixed, fills applied, how long
   blind, time to recover.

### 7. Safety mode + alerting
- **Safe mode:** a **persisted** global flag gating *all* order submission (in addition to
  `ENABLE_LIVE_ORDERS`). Workers keep observing/logging but submit nothing. Surfaced in the UI.
  Clears only via an explicit manual **resume** endpoint after human review.
- Complements the existing **kill switch**: safe mode = "open nothing, hold for review";
  kill switch = "cancel everything + flatten."
- **Alerting:** on disconnect, on entering safe mode, on prolonged reconnect failure → journal
  events + UI status + structured log, via a **pluggable alert channel** (default: journal/UI/
  log; email/webhook can be added later without touching call sites).

## Data flow

```
                ib_async events / heartbeat
                          │
                  ConnectionManager ── publishes state ──► workers (pause/resume), UI
                          │ (→ CONNECTED)
                          ▼
                  RecoveryCoordinator
        ┌─────────────────┼─────────────────────────┐
        ▼                 ▼                          ▼
 ReconciliationService  BackfillService     Durable state rebuild
  (orders/fills/pos)    (gap bars + re-sub)  (+ cross-check broker)
        │                                            │
        └──► unreconciled? ── yes ──► SAFE_MODE + alert (stop)
                 │ no
                 ▼
              READY → resume workers → recovery report → journal
```

## Error handling
- Reconnect failures: keep retrying with backoff; after a (configurable) prolonged outage,
  alert but keep trying — never silently give up.
- Reconciliation/backfill exceptions during recovery are **fail-safe**: any unhandled error in
  the recovery sequence trips SAFE_MODE rather than resuming.
- Pacing violations during backfill: backoff + retry; partial backfill leaves the gap gate
  closed (strategy stays paused) rather than acting on incomplete data.

## Testing
All offline against the **stubbed `IB`** (the gateway already injects `ib_factory`):
- Connection: simulated `disconnectedEvent` + `1100/1101/1102`; heartbeat-miss → reconnect;
  backoff schedule.
- Reconciliation: orphan fills from `reqExecutions`, position mismatches, partial fills →
  reconciliation-matrix **table tests** (every cell); assert fills applied vs SAFE_MODE.
- Idempotency: submit → "disconnect before ack" → reconnect → assert **no resend** (matched by
  `orderRef`); stuck-`submitting` resolution.
- Backfill: stub provider returning a gap → assert merged/contiguous + requests serialized
  (pacing); gap gate keeps strategy paused until contiguous.
- Durable state: write snapshot → simulate restart → assert `State` rebuilt + cross-checked.
- Recovery sequence: full boot-order integration test, including SAFE_MODE short-circuit.
- One **manual** paper-Gateway smoke for the live path (like `connect_smoke.py`).

## Phasing (for the implementation plan)
- **Phase 1 (safety-critical core):** ConnectionManager (detect + reconnect + state machine),
  idempotent orders, ReconciliationService + matrix, durable strategy state, SAFE_MODE gate,
  RecoveryCoordinator startup sequence. Delivers "never trade on a wrong picture."
- **Phase 2 (continuity + ops):** BackfillService + live re-subscribe, pluggable alert channel,
  UI surfacing of connection/safe-mode state, recovery-report view.

## Implementation caveats
- Verify exact ib_async **2.1.0** event/method names during build (`reqAllOpenOrdersAsync`,
  `reqExecutionsAsync`, `reqPositionsAsync`, `commissionReportEvent`, `reqCurrentTimeAsync`,
  and whether to use the built-in `ib_async.Watchdog` vs a custom heartbeat).
- IBKR positions/executions are **account-wide** — reconciliation must scope to orders this
  system placed (via `orderRef`) and treat *other* account activity as "unknown" (→ SAFE_MODE),
  not adopt it.
- Keep the existing public execution API stable so workers and tests don't churn.
