# Builder visual option chain (tastytrade-style) — design

**Date:** 2026-07-10
**Status:** Approved (brainstorm) → implementation

## Goal

Restyle the Builder tab's option-picking and payoff presentation to match the reference
screenshot (tastytrade/Robinhood-style): month-grouped expiry tab bar, horizontal strike
ruler with a C/P selection badge, a stats row (net debit / max loss / max profit /
breakeven), a red/green gradient payoff chart with breakeven marker, and RANGE ±% /
IMPLIED VOLATILITY sliders.

**Decisions (from brainstorm):** all screenshot elements EXCEPT the date scrubber
(value-through-time slider) — explicitly not selected, YAGNI. The new UI is a **visual
layer on top**: the legs table, quoted near-ATM chain grid, and save-as-paper-trade flow
all stay. Approach A: React components + Recharts gradient upgrade (no canvas rewrite).
**Backend changes: none** — chain/quote/payoff endpoints already provide everything.

## Current state

`frontend/src/components/BuilderTab.jsx` (177 lines): underlying input → `/api/builder/chain`
(`{expirations: [...iso], strikes: [...]}` — Tastytrade-served via the facade), legs table
(dropdown selects), "Fetch quotes" → net debit, `/api/builder/payoff` (Black-Scholes)
→ `PayoffChart.jsx` (34-line Recharts line chart, `{curve: [{price, pnl}], breakevens: [...]}`),
`ChainGrid.jsx` (expiry pills + quoted grid), save-and-track (paper only, never live orders).

## Components (new files under `frontend/src/components/builder/`)

### `ExpiryTabs.jsx`
- Props: `{ expirations: string[] (ISO), value: string, onChange(iso) }`.
- Groups expirations by month → top row of month segments ("Jul", "Aug", … "Jan '27" when
  the year changes), bottom row of day numbers under their month. Selected day = blue pill
  (`.pill.live`-style accent); months with no selectable day are dimmed. Horizontal
  scroll (`overflow-x: auto`) for long lists; auto-scroll the selected day into view.
- Pure presentation; no fetching.

### `StrikeRuler.jsx`
- Props: `{ strikes: number[], value: number, right: "C"|"P", action: "BUY"|"SELL",
  spot: number|null, onChange(strike) }`.
- Horizontal ruler: one tick per strike, taller tick + label every Nth (N chosen so labels
  don't collide — roughly every $20 for QQQ-like chains), scrollable, auto-centered on
  `value` (or ATM when first rendered). Click a tick → `onChange(nearest strike)`;
  drag (pointer events with `setPointerCapture`) scrubs the selection with snapping.
- Badge above the current tick: `{strike}{right}` (e.g. **725C**), green background when
  `action === "BUY"`, red when `SELL` — matching the screenshot's strike flag.
- A thin marker at `spot` when provided.

### `StatsRow.jsx`
- Props: `{ netDebit, qty=1×100 scaling handled by caller, stats }` where
  `stats = computeStats(curve, breakevens, spot)` (see below). Renders the five-column
  strip: NET DEBIT (or NET CREDIT when negative), MAX LOSS, MAX PROFIT, BREAKEVEN
  ("Above $749.56 (+3.3%)" / "Below … (−…%)" / two breakevens → "…$A / $B…").
  Values formatted `$X,XXX.XX`; "Infinite" for unbounded sides. No "chance of profit"
  (screenshot shows it locked; not built).

### `payoffStats.js` (pure functions — the only tested logic)
```
computeStats(curve, breakevens, spot) -> {
  maxProfit: number | "infinite",
  maxLoss: number | "infinite",
  breakevens: [{ price, movePct }],
}
```
- Let `slopeR` = pnl slope per $1 of underlying over the LAST two curve points, and
  `slopeL` = the slope over the FIRST two points. `eps = 0.01` (per-share).
- **Right edge (price → ∞) is genuinely unbounded:** `slopeR > eps` → maxProfit =
  `"infinite"`; `slopeR < −eps` → maxLoss = `"infinite"` (short-call-style).
- **Left edge (price → 0) is always bounded:** extrapolate linearly from the first two
  points down to price 0 and include that extrapolated pnl when taking max/min. A long
  put's "max profit at zero" and a short put's "max loss at zero" therefore show as
  finite dollar values, never "Infinite".
- Otherwise maxProfit = max pnl over the (extended) curve, maxLoss = min pnl.
- movePct = (breakeven − spot) / spot.
- All per-share values ×100×qty by the caller (matches the project's P&L convention).

### `PayoffChart.jsx` (upgraded in place)
- Recharts `AreaChart`: single series, gradient fill split at the zero crossing
  (`<defs><linearGradient>` with offset = gradientOffset(curve): green above 0, red below,
  like the screenshot's fade). Zero reference line; dotted vertical `ReferenceLine` at each
  leg strike; blue vertical `ReferenceLine` + top label at each breakeven; spot marker.
- Props extended: `{ curve, breakevens, strikes: number[], spot, window: [lo, hi] }` —
  the chart clips its x-domain to `window`.

### Sliders row (in `BuilderTab.jsx`)
- **RANGE ±%**: client-side only; sets the chart x-window to `spot × (1 ± pct)`,
  default ±8%, min ±2%, max ±50%. No refetch.
- **IMPLIED VOLATILITY**: the existing `iv` input becomes a slider (5%–150%, default from
  the current quote flow); on release (`onMouseUp`/`onPointerUp`, not every tick) re-calls
  `api.builderPayoff({legs, net_debit, iv})`.

### Header touch (in `BuilderTab.jsx`)
- Next to the underlying input: last price + day-change chip (green/red), from the existing
  quote fetch (`/api/builder/chain-quotes` spot or a `stock_quote`-backed endpoint the tab
  already calls when fetching quotes). No new polling loop; refresh on "Fetch quotes".

## Selected-leg model (BuilderTab.jsx)

- New state `selectedLeg: number | null` (index into `legs`). Clicking a legs-table row
  selects it (row highlight); `addLeg` selects the new leg; deleting the selected leg
  selects the previous one (or null when empty).
- `ExpiryTabs.onChange` / `StrikeRuler.onChange` mutate ONLY the selected leg (via the
  existing `setLeg(i, patch)` + `invalidateQuote()` so stale net-debit can't linger —
  preserves the existing stale-quote fix).
- The ruler's badge reflects the selected leg's `right` + `action`; toggling right/action
  still happens in the legs table (no duplicate controls).

## Layout (top to bottom, matching the screenshot's order)

1. Header: underlying input + price chip
2. `ExpiryTabs`
3. `StrikeRuler`
4. `StatsRow`
5. `PayoffChart` (gradient)
6. Sliders row (RANGE / IV)
7. Existing: legs table → quoted `ChainGrid` → net debit / target-stop / Save section

## Error handling

- No chain yet (blank/unknown underlying): tabs/ruler render disabled skeletons; stats show
  "—". Payoff absent → chart placeholder text (existing behavior).
- `spot` null (both data sources down): ruler centers on the middle strike, stats omit
  movePct, chart window falls back to full curve range.
- Odd curves (single point, all-zero) must not crash `computeStats` — return "—" values.

## Testing

- `payoffStats` pure-function tests (vitest if configured, else a small node script run in
  the build check): long call → maxProfit infinite / maxLoss = debit; short call →
  maxProfit = credit / maxLoss infinite; long put → maxLoss bounded via price-0
  extrapolation; calendar-style bounded curve → both finite; breakeven movePct sign.
- Frontend build must pass (`VITE_API_BASE="" npm run build`).
- Manual: QQQ — pick expiry from tabs, scrub the ruler, verify badge/stats/gradient/
  breakeven line match a hand-checked example; save flow unchanged.

## Non-goals

- No date scrubber (value through time) — not selected.
- No "chance of profit" (needs a prob model; screenshot shows it locked anyway).
- No live orders from the Builder (unchanged invariant).
- No backend changes; no changes to ChainGrid or the save/track flow.
