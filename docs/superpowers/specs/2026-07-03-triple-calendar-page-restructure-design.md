# Triple Calendar page restructure — design

**Date:** 2026-07-03
**Status:** Approved (brainstorm) → implementation

## Problem

The Triple Calendar strategy (`strategy_2`, `is_options = True`) renders through the
generic `StrategyDetail.jsx`, which was built for equity strategies. Concretely, today's
page is wrong in three ways:

1. **Misleading "Backtest" section.** `GET /api/strategies/strategy_2/backtest` returns a
   **SPY buy-and-hold baseline** (returns, win%, Sharpe, equity curve). None of it relates
   to the calendar spread. It reads as if it were the strategy's own backtest.
2. **No options info.** The page never shows the position's legs, strikes, sell/buy
   expiries, DTE, or net debit — the things that actually describe a Triple Calendar.
3. **No live position / P&L.** `StrategyDetail` reads the equity `TradeRecord` path
   (`position` / `/{key}/position`), which is always empty for TC. The live position lives
   in `OptionPositionRecord`, which the page never touches. `has_position` is always
   `false` here.

## Goal

A dedicated, options-aware page for Triple Calendar that shows: the open position's
structure (legs/strikes/expiries/DTE/net debit), **live dollar P&L and progress toward the
+10% target / −25% stop**, breach distances, and live operational status (connection READY,
RTH open, loop active, live-orders on/off). Remove the irrelevant backtest section for
options strategies. Equity strategies are untouched.

## Key insight (de-risks live P&L)

`triple_calendar.strategy.manage()` **already computes** the live position value and P&L%
every tick (every `tc_manage_interval_seconds`, default 300s) to make its own target/stop
decisions — it just discards the number. So live P&L needs **no new IBKR fetching and no
slow endpoint**: the live loop persists the value it already computes; the page reads it.
This also guarantees the displayed P&L exactly matches what the strategy acts on.

## Design

### A. Make the page options-aware (routing)

- Add `is_options: bool = False` on `BaseStrategy`, and include it in `overview()` output
  (TC already declares `is_options = True` on the class).
- `StrategyDetail.jsx`: when `overview.is_options` is true, delegate rendering to a new
  `OptionsStrategyDetail.jsx`. No hardcoded `strategy_2`.
- The generic equity backtest section is **not rendered** for options strategies.

### B. Backend — persist live valuation, expose an options-position endpoint

**DB (`OptionPositionRecord`)** — add 4 nullable columns:
`last_value` (Float), `last_pnl_pct` (Float), `last_spot` (Float), `valued_at` (DateTime).
Add them via the existing additive-migration pattern in `db/base.py::_ensure_added_columns`
(`ALTER TABLE option_positions ADD COLUMN ...`, guarded by an `inspect()` existence check).

**Live loop (`strategy_class.run_live`)** — after each `manage_step` tick, while a position
is open and `record_id` is set, compute `value = position_value(snapshot, position, today)`
(pure, in-memory, no I/O — reuses the tick's `snapshot`) and persist
`value`, `pnl_pct = (value − net_debit)/net_debit`, `spot`, `valued_at = now`. Skip when
`value` is `None`. New `store.update_value(record_id, value, pnl_pct, spot, valued_at)`.

**Endpoint** — `GET /api/strategies/{key}/options-position` (auth-gated read like the other
strategy reads; `_require_ready`). **Zero IBKR calls.** Reads the open `OptionPositionRecord`
plus engine/connection state and returns:

```
{
  has_position: bool,
  underlying, status, paper, entry_time, days_held,
  net_debit,
  calendars: [ { strike, sell_expiry, buy_expiry, sell_dte, buy_dte } ],   # DTE = expiry − today
  target_value:  net_debit * (1 + profit_target),   # +10%
  stop_value:    net_debit * (1 - stop_loss_pct),   # −25%
  # live-valued (persisted by the loop; null when never valued / flat):
  value, unrealized_pnl, unrealized_pct, spot, valued_at, stale: bool,
  progress_to_target,   # 0 at entry, 1 at +10% target
  distance_to_stop,     # fraction of the way from entry toward the −25% stop
  breach: { lower_strike, upper_strike, dist_lower_pct, dist_upper_pct },  # from spot; null if no spot
  # operational status:
  running, phase, broker_connected, conn_ready, market_open,
  live_orders_enabled, last_trade_time, last_signal_time
}
```

- `unrealized_pnl = (value − net_debit) * 100 * contracts` in dollars (calendar debit is per
  share; ×100 per contract). v1 assumes size = 1 contract (matches current live loop);
  document the assumption.
- `stale = valued_at older than ~2 manage intervals` (loop hasn't ticked recently, e.g.
  market closed / disconnected). Surfaces a "stale" badge instead of a misleading fresh P&L.
- All live-valued fields are `null` when flat, never-valued, or disconnected — the UI
  degrades to structure-only.

### C. Frontend — `OptionsStrategyDetail.jsx`

Dark dashboard styling, indigo accent, consistent with the rest of the app.

1. **Header** — name · status pill (Live/Idle) · Paper/Live badge.
2. **Status strip** — small pills: `READY` (connection), `RTH open`, `Loop active`,
   `Live orders on/off`. The "is it actually working right now" row.
3. **Position card** (when `has_position`):
   - Underlying · entered date (N days held) · net debit.
   - **Current value + unrealized $ / %** as the big colored figure (green/red).
   - **Progress-to-target bar** (0 → +10%) and **stop-distance meter** (toward −25%).
   - **Legs table**: calendar # · strike · sell exp (DTE) · buy exp (DTE).
   - **Breach row**: spot vs lower/upper strike, distance %.
   - `valued_at` timestamp + "stale" badge when the loop hasn't ticked recently.
   - When **flat**: "Scanning for entry" empty state — underlying, entry criteria, last
     trade time.
4. **Controls card** — allocation + Activate/Stop (reused logic), options-flavored copy,
   params readout (target / stop / VIX thresholds).
5. **Overview card** — rules / entry / exit (kept).

Meters are plain CSS bars (no chart / no value-over-time history in v1).

## Testing

- **Backend endpoint (new test)**: with a seeded open `OptionPositionRecord` (calendars +
  `last_value`/`valued_at`), `/options-position` returns `has_position: true`, correct DTE
  (expiry − today), `target_value`/`stop_value`, `progress_to_target`, and breach distances;
  with no open record returns `has_position: false`. Extend `tests/` in the existing style.
- **Loop persistence**: unit-test that a hold tick with a resolvable snapshot writes
  `last_value`/`last_pnl_pct`/`last_spot`/`valued_at` onto the record (via a fake snapshot +
  in-memory position, no IBKR).
- **Migration idempotency**: `init_db()` twice on a fresh SQLite DB doesn't error and the 4
  columns exist.
- **Manual**: run the web app, open Triple Calendar, confirm no SPY backtest, status strip
  renders, flat empty-state shows; if a position is open, legs/DTE/live P&L render.

## Scope / non-goals

- No position-value-over-time chart (meters only) in v1.
- Multi-contract sizing display is v1-assumed at 1 contract (matches the live loop today).
- No changes to equity strategy pages or the generic backtest for equities.
