# IBKR Resilience — Phase 2a (Data Continuity) Design

> **Status:** approved design (2026-06-29). Backend-only sub-project of Phase 2. Phase 2b
> (alerting + UI surfacing) is a separate spec. Builds on Phase 1
> (`2026-06-28-ibkr-resilience-design.md`, implemented on `main`).

## Problem

Phase 1 detects disconnects, reconnects, reconciles broker truth, and gates trading on
`ConnState.READY`. Two data-continuity gaps remain:

1. **Workers can act on an incomplete price series.** After a mid-session outage a worker may
   resume with a hole in its bars (a missing latest bar), yet nothing stops it from acting on
   that stale/gapped series.
2. **A lazy reconnect can strand the system.** `Gateway.get_ib()` auto-reconnects for any caller
   (`market.py`, `account.py`, `execution.py`) by calling `connect()`, which lands in
   `CONNECTED` — but **nothing drives `CONNECTED → READY`**. The supervisor only runs recovery
   from `DISCONNECTED`. So a `get_ib()` reconnect leaves the system connected-but-paused forever.

Note: workers already refetch bars through `history_cache.get_or_fetch` on a TTL, so a *long*
outage (cache past TTL) self-heals on the next cycle. The gaps above are the missing safety
(don't act on a hole) and the missing correctness (always reach READY after a reconnect), plus a
**short-outage** stale window (cache still within TTL → stale bars served until TTL expires).

## Goal

After any reconnect: the system reliably reaches `READY`, active symbols' caches are refreshed
promptly, and **no worker acts on a non-contiguous series** — each symbol gates independently
(per-symbol gating, the chosen posture; one slow/pacing-limited symbol never blocks others).

## Approach (chosen: A — lean; reuse the existing cache machinery)

Add a per-symbol **gap gate** to the worker loops, a light **proactive backfill** that
force-refreshes active symbols on reconnect, and a central **seam fix** in the supervisor that
drives `CONNECTED → READY`. Reuse `IBKRHistoricalProvider` + `history_cache` rather than building
a parallel fetch/merge stack.

Rejected: **B** (dedicated BackfillService computing exact missing ranges with a pacing queue) —
significant overlap with `get_or_fetch` for marginal gain at this scale. **C** (seam + gate only,
no proactive refresh) — leaves a needless stale window after short outages.

## Components

### 1. Gap gate — `app/services/backfill.py` (pure)
```
def is_stale(bars: list[dict], interval_seconds: int, now_ms: int, *, market_open: bool) -> bool
```
- `market_open` False → return **False** (closed: no new bars expected; overnight gaps are normal).
- `market_open` True → return **True** when `bars` is empty OR the last bar's `time` (ms) is older
  than `1.5 * interval_seconds * 1000` before `now_ms` (the latest closed bar is missing).
- Pure, no I/O → table-testable. `market_open` comes from the existing
  `app/services/market.py::get_market_status()["is_open"]`; the gate helper itself takes the bool
  so it stays pure.

### 2. Proactive backfill — `app/services/backfill.py`
```
async def backfill_active() -> None
```
- Read active `StrategyState` rows (`status in ("scanning","active")`).
- For each strategy in the registry exposing an **optional** `backfill_specs() -> list[tuple[str,str]]`
  (`(symbol, interval)`), force-refresh each spec: `bars = await provider.get_bars(symbol, interval,
  start=...)` then `history_cache.save(symbol, interval, bars, source=provider.name)` — **bypassing
  the TTL** so even a within-TTL cache is updated.
- **Sequential** with a small `await asyncio.sleep(delay)` between requests (pacing-friendly;
  IBKR ~60 req/10 min). Best-effort: a per-spec exception is logged and skipped, never raised.
- Strategies without `backfill_specs()` are skipped — their workers' normal TTL refetch covers them.
- Phase 2a implements `backfill_specs()` on the rule-driven scalper (selected symbol + candle
  interval) and the crossover (watchlist symbols + their intervals).

### 3. Reconnect-seam fix — `Gateway.supervise()`
Extend the supervisor tick: when the socket `is_connected()` **and** `state is ConnState.CONNECTED`
(reconnected by a lazy `get_ib()` without recovery), run `run_recovery(self._ib, self)` → drives to
`READY` or `SAFE_MODE`. `get_ib()` is unchanged; the ≤ one-tick window before the supervisor catches
it is safe because workers stay paused while not `READY`. The existing `DISCONNECTED` branch
(reconnect_loop → recovery) is unchanged.

### 4. Backfill trigger in recovery — `recovery.run_recovery`
On a successful recovery (just before/after setting `READY`), launch
`asyncio.create_task(backfill_active())` — non-blocking, so READY (and per-symbol gating) is not
delayed by a pacing-limited backfill. Failure inside the task is contained (best-effort).

### 5. Worker integration — `app/engine/worker.py`
In the rule-driven and multi-position loops, after obtaining a symbol's `bars`, compute
`stale = is_stale(bars, interval_seconds, now_ms, market_open=get_market_status()["is_open"])`; if
stale, **skip acting** on that symbol this cycle (log once at debug/info: "awaiting backfill
<symbol>") and continue. The generic loop is unaffected (not bar-driven). The interval seconds come
from the strategy's candle interval (e.g. `5m` → 300).

## Data flow

```
reconnect (supervisor: DISCONNECTED→reconnect, or CONNECTED→ here)
        │
   run_recovery → READY ──► asyncio.create_task(backfill_active())
        │                        │ sequential, pacing-friendly
        ▼                        ▼ force-refresh (symbol,interval) via provider→history_cache.save
   workers resume (READY)   cache freshened
        │
   each cycle: fetch bars (get_or_fetch) → is_stale? ── yes ──► skip symbol (await backfill)
                                              │ no
                                              ▼ act normally
```

## Error handling
- `backfill_active` is best-effort: per-spec exceptions are logged and skipped; the task never
  raises into recovery. A failed backfill just means the worker's own gap gate keeps that symbol
  paused until its cache catches up.
- Pacing: sequential fetch + inter-request delay; a pacing rejection is treated as a skip (gate
  stays closed for that symbol).
- Seam: if `run_recovery` from the supervisor trips `SAFE_MODE`, workers stay paused — same as any
  recovery, no special-casing.

## Testing (all offline / stubbed)
- **Gap gate** (`test_gap_gate`): table tests — market closed → not stale; open + empty bars →
  stale; open + last bar older than 1.5× interval → stale; open + fresh last bar → not stale.
- **backfill_active** (`test_backfill_active`): stub provider + an active `StrategyState` whose
  strategy returns `backfill_specs()` → assert each spec force-refreshed (provider called, cache
  saved) and requests are sequential.
- **Seam** (`test_supervise_seam`): a `Gateway` with a stubbed connected IB and `state==CONNECTED`
  → one supervisor step runs recovery → `READY`.
- **Worker gate** (`test_worker_gap_gate`): rule-driven worker fed stale bars with market open →
  asserts it does not act (no order / no trade record) and logs the skip; with fresh bars it acts.
- One **manual** paper-Gateway smoke: disconnect mid-session, confirm reconnect → recovery →
  backfill refreshes bars → workers resume only once contiguous.

## Out of scope (Phase 2b — separate spec)
Pluggable alert channel (email/webhook), UI surfacing of connection/safe-mode state + recovery
report, and auto-applying confirmed missed close-fills. Live market-data re-subscription is **not
needed** — IBKR quotes here are one-shot (`reqMktData`/`cancelMktData` in `market.py`), so there is
no persistent subscription to restore.

## Implementation notes
- `is_stale` takes `market_open` as a bool (stays pure); callers pass
  `get_market_status()["is_open"]`.
- `backfill_specs()` is additive and optional on `BaseStrategy` (default returns `[]`), so existing
  strategies and tests are unaffected.
- Interval→seconds mapping (`"1m"`→60, `"5m"`→300, `"15m"`→900, `"1h"`→3600, `"1d"`→86400) lives
  next to the gate helper.
