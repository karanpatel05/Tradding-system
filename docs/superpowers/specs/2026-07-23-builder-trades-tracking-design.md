# Builder trade-tracking view — design

**Date:** 2026-07-23
**Status:** approved (design), pending spec review → implementation plan
**Branch:** `builder-trades-tracking`

## Problem

The Options Builder already saves paper trades, tracks their live P&L (a background
monitor revalues open trades), and draws a payoff diagram — but only for the structure
*currently being built*. Saved trades appear in a "Recent Trades" list showing P&L numbers
with **no payoff graph** and **no portfolio-level totals**. The user wants a redesigned
trade-tracking view: a per-trade payoff graph, an aggregate P&L summary, and a fuller
tracking layout.

## Goals

Within the Builder tab, a **Build | Trades** sub-view. The **Trades** view shows:

1. An **aggregate P&L summary** across all saved trades (net / realized / unrealized,
   open+closed counts, win rate).
2. The **saved-trades list** (moved out of the Build workspace).
3. A **per-trade payoff detail** — click a trade to expand its payoff diagram + breakevens /
   max-P&L + live P&L + net greeks. Works for open and closed trades.

## Non-goals (v1)

- No backend changes — every needed endpoint already exists (see "Reused APIs").
- Chance-of-profit per saved trade (the saved-trade endpoint doesn't return it).
- Editing a saved trade's legs / re-pricing at a new IV.
- A separate top-level nav tab (the toggle keeps it inside Builder).

## Reused APIs (all existing — no new endpoints)

- `GET /api/builder/trades` → all trades; each carries `status`, `pnl` (closed),
  `last_pnl` / `last_pnl_pct` / `last_value` (open, from the monitor). Aggregate is computed
  **client-side** from this list.
- `GET /api/builder/trades/{id}` → the trade + `curve` (payoff) + `eval_date` +
  `max_profit` / `max_loss` / `breakevens`.
- `GET /api/builder/spot?underlying=` → live spot for the chart's spot marker.
- `POST /api/builder/greeks` `{underlying, legs, spot, iv}` → net greeks for the trade.
- `POST /api/builder/trades/{id}/close` → close an open trade.

## Architecture

### Structure — `BuilderTab.jsx` (modified)

Add a `view` state (`"build" | "trades"`, default `"build"`) and a toggle at the top of the
tab. `view === "build"` renders the existing workspace unchanged (wrapped in a fragment).
`view === "trades"` renders `<BuilderTrades />`. The current "Recent Trades" section (and its
`trades`/`loadTrades`/`closeTrade` state) **moves out** of `BuilderTab` into `BuilderTrades`,
which also trims the 522-line file. The "Save as tracked trade" control stays in the Build
workspace; on a successful save, `BuilderTab` switches `view` to `"trades"` so the saved trade
is visible.

### `frontend/src/components/BuilderTrades.jsx` (new)

Owns saved-trades loading (`api.builderTrades()`, polled every 15s), the aggregate summary,
the list, and the selected-trade detail. Composed of:

- **Summary tiles** — a `deploy-grid` of stat tiles (same styling as the strategy pages, not
  the build-preview `StatCards`) fed by `summarizeTrades(trades)`.
- **Trade list** — the existing trade cards. Clicking a card sets `selectedId` (toggle to
  collapse). The selected card highlights.
- **Detail panel** — rendered inline below the selected card (accordion). On select, fetches
  `builderTrade(id)`, `builderSpot(underlying)`, and `builderGreeks({...})` in parallel, then
  renders `<PayoffChart curve breakevens strikes spot />` + a stat row (breakevens · max
  profit · max loss · live P&L) + `<GreeksPanel greeks={...} />`. `strikes` = the option legs'
  strikes; live P&L = `last_pnl` (open) or `pnl` (closed).

### `frontend/src/components/builder/tradesSummary.js` (new, pure)

```
summarizeTrades(trades) -> {
  net,          // realized + unrealized
  realized,     // Σ closed.pnl
  unrealized,   // Σ open.last_pnl
  openCount, closedCount,
  winRate       // closed winners (pnl > 0) / closedCount, or null when closedCount === 0
}
```

Pure, null-safe (treats missing `pnl`/`last_pnl` as 0). Unit-tested.

## Data flow

`BuilderTrades` mounts → `loadTrades()` (poll 15s) → `summarizeTrades` feeds the tiles and the
list renders. User clicks a trade → parallel fetch of detail/spot/greeks → detail panel
renders. Live P&L in the tiles, list, and open detail refreshes on the 15s trade poll; the
payoff curve itself is static per trade (recomputed only on re-select).

## Error handling

Each fetch degrades independently (existing `.catch(() => {})` pattern): a failed spot fetch
drops the marker but keeps the curve; a failed greeks fetch hides the greeks panel; a failed
detail fetch shows "Couldn't load payoff." The summary and list never break on one bad trade.

## Testing

- `frontend/src/components/builder/tradesSummary.test.mjs` — plain-node test (matching
  `payoffStats.test.mjs`; run via `node <path>`). Cases: empty list → zeros + `winRate null`;
  mixed open/closed → correct net/realized/unrealized/counts; win rate with winners and
  losers; null-safe on missing pnl fields.
- `npm run build` — compiles clean (no JS test harness for React components).

## Files touched

- `frontend/src/components/BuilderTrades.jsx` (new)
- `frontend/src/components/builder/tradesSummary.js` (new)
- `frontend/src/components/builder/tradesSummary.test.mjs` (new)
- `frontend/src/components/BuilderTab.jsx` (toggle; move trades rendering out)
