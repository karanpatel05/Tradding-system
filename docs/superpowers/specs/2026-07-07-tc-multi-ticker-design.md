# Multi-ticker Triple Calendar — design

**Date:** 2026-07-07
**Status:** Approved (brainstorm) → implementation

## Goal

Run the Triple Calendar on **multiple tickers at once**, each with its own independent position
(enters / rolls / exits / adjusts on its own) and its own contracts size. The ticker list is
**editable in the UI** (add/remove, per-ticker contracts), 2–5 tickers in practice.

Today TC is hardwired to one underlying (`tc_underlying`), holds one position, and the loop /
store / endpoint / UI all assume a single position.

## Feasibility (already verified)

No architectural blocker: `OptionPositionRecord` keys on `(strategy_key, underlying)` with **no
unique constraint** (multiple open records per strategy already storable); `flatten_all` (kill
switch) closes **every** open IBKR option leg; execution places an independent combo per call;
the equity strategies already run multi-position. Only TC's *logic* assumes one position
(`load_open`/`load_open_id` use `.first()`, `run_live` holds a single `position`, the endpoint
returns the first record) — this design generalizes those to "one position per ticker."

## Data model

- `StrategyState.tc_tickers`: JSON list of `{"ticker": str, "contracts": int}` (additive
  migration: `ALTER TABLE strategy_state ADD COLUMN tc_tickers JSON`; default `[]`).
- **Fallback:** when `tc_tickers` is empty, behavior is unchanged — the loop/endpoint use a
  single implicit entry `{ticker: tc_underlying, contracts: StrategyState.contracts or 1}`. So
  existing single-ticker behavior is preserved with no config.
- The per-ticker `contracts` in `tc_tickers` is authoritative for TC sizing. The global
  `StrategyState.contracts` control (prior feature) now applies **only** to the empty-list
  fallback; the UI labels it accordingly.

## Store — key by underlying (`store.py`)

- `load_open(strategy_key, underlying) -> Position | None` — add the `underlying` filter.
- `load_open_id(strategy_key, underlying) -> int | None` — add the `underlying` filter.
- `list_open(strategy_key) -> list[OptionPositionRecord]` — all open records for the strategy
  (used by the endpoint and startup recovery).
- `save_position` already stores `underlying` + `contracts`; `close_position`/`update_value`/
  `update_calendars` operate by `record_id` (unchanged).

**Migration note:** the `underlying` filter changes `load_open`'s signature. All callers
(`run_live`) are updated in this change; there are no other callers.

## Live loop (`strategy_class.run_live`)

- `_params(underlying)` — parameterize the helper by underlying (shared `event_dates`, per-ticker
  `underlying`). A `_tickers()` helper reads `StrategyState.tc_tickers`; empty → the fallback
  single entry.
- Per-ticker state: `positions: dict[str, Position | None]` and `record_ids: dict[str, int | None]`,
  keyed by ticker. On startup, `list_open` seeds them.
- Each tick (when READY + market open): fetch **VIX once** (global gate). Then **for each
  configured ticker**: resolve its snapshot (its spot/chain/mids), `manage_step(today, snapshot,
  params_for_ticker, executor, positions[t], vix=vix, contracts=<ticker contracts>)`, persist per
  ticker (save/close/update keyed by that ticker's record), and update its live valuation. A
  per-ticker try/except so one ticker's error doesn't abort the others.
- **Fail-closed** (flat + VIX unreadable → skip that ticker's entry) is per-ticker.
- **Adaptive cadence:** `poll = vix_wait_poll_seconds` (5s) if **any** configured ticker is flat
  **and** (`vix is None or vix > vix_entry_max`); else `tc_manage_interval_seconds` (300s). The
  not-READY / market-closed pause is unchanged.
- Roll-on-exit, breach-adjust, and the VIX ≤ 16 gate all operate per ticker (VIX/events are
  market-wide and shared).

## Endpoint (`routers/strategies.py`)

Replace the single-position `/options-position` payload with a per-ticker list:

```
{
  "tickers": [
    { "ticker": "QQQ", "contracts": 2, "has_position": true, <all the current position fields:
        underlying, status, paper, entry_time, days_held, net_debit, calendars, target_value,
        stop_value, profit_target_pct, stop_loss_pct, vix_threshold, value, unrealized_pct,
        unrealized_pnl (×that ticker's contracts), spot, valued_at, stale, progress_to_target,
        distance_to_stop, breach> },
    { "ticker": "SPY", "contracts": 1, "has_position": false }
  ],
  "running", "broker_connected", "conn_ready", "market_open", "live_orders_enabled",
  "last_signal_time", "last_trade_time"
}
```

- Build each ticker entry with the **existing** per-position payload logic (extract it into a
  helper `_serialize_option_position(rec, contracts)`), so a single ticker renders exactly like
  today.
- The ticker list = `tc_tickers` if non-empty, else the single fallback entry. For each ticker,
  the open `OptionPositionRecord` (by `underlying`) or `has_position: false`.
- **`GET`/`PUT /api/strategies/{key}/tickers`**: `PUT` body `{tickers: [{ticker, contracts}]}` —
  uppercase tickers, contracts clamped ≥ 1, de-duplicated, capped at 10; sets
  `StrategyState.tc_tickers`. Empty list reverts to fallback. `detail` response includes
  `tc_tickers`.

## Frontend (`OptionsStrategyDetail.jsx`)

- A **Tickers** card: rows of `{ticker input, contracts input, remove}` + "add ticker" + "Save
  tickers" (calls `api.setTickers(key, list)`). Seeded from `detail.tc_tickers` (or the fallback
  single ticker). `api.js`: `setTickers(key, tickers)`.
- The **Open Position** section becomes **one card per ticker** (map over the endpoint's
  `tickers` list): a full position card when `has_position`, else a compact "scanning" row. The
  existing single-position card markup is reused per ticker.
- The global "Contracts" control stays but is labeled "default (single-ticker mode)"; per-ticker
  contracts live in the Tickers card.

## Testing

- **Store:** `load_open(key, underlying)` / `load_open_id(key, underlying)` return only that
  underlying's open record; `list_open(key)` returns all open records; two tickers with open
  positions are isolated.
- **Loop (live-style, fakes):** two configured tickers both flat → each enters its own position
  with its own contracts (assert two open `OptionPositionRecord`s with correct `underlying` +
  `contracts`); a per-ticker error doesn't stop the other ticker.
- **Endpoint:** `PUT /tickers` clamps/uppercases/dedupes/caps and persists; `/options-position`
  returns one entry per configured ticker (open + flat), P&L scaled by each ticker's contracts;
  empty `tc_tickers` → single fallback entry (back-compat).
- **Migration:** `init_db()` twice idempotent; `tc_tickers` column exists, defaults to `[]`.
- **Manual:** add SPY + IWM in the UI, confirm the endpoint returns three ticker entries and the
  UI shows a card per ticker; (positions fill during RTH with the Gateway connected + VIX ≤ 16).

## Non-goals

- No cross-ticker portfolio risk sizing (each ticker is independent; contracts is explicit).
- No per-ticker VIX/event overrides (VIX ≤ 16 and event dates are market-wide/shared).
- No options backtest (none exists for TC; paper/live only).
- Cap at ~10 tickers to bound per-tick quote fan-out (2–5 expected).
