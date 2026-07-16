# Premium terminal redesign (Bloomberg/OptionStrat-class) — design

**Date:** 2026-07-16
**Status:** Approved (brainstorm) → implementation

## Goal

Redesign the dashboard into a premium, institutional-grade terminal: a coherent dark
design system (deep navy, glassmorphism, electric-blue accent, 16px radius, micro-
interactions), a rebuilt Builder page (stat cards, sleek expiry picker, interactive
strike slider, centerpiece payoff chart with zoom/pan, modern legs table with templates
and hedge actions), an upgraded sidebar — plus professional analytics: Greeks panel,
Greeks chart overlay, and an implied-volatility smile chart.

## Decisions (from brainstorm)

- **Two phases, one spec, built in order.** Phase 1 = visual system + workflow
  features. Phase 2 = analytics (Greeks + IV smile). The app ships working after every
  task.
- **Stock legs are added** (leg type `S`) so Covered Call templates work properly.
  The Builder remains strictly paper — stock legs never reach the order layer (the
  existing no-transmit invariant covers them by construction).
- **Approach: design-token overhaul in place.** CSS custom properties + restyle of
  existing components; Recharts stays; animations via CSS only. No new frontend
  dependencies, no framework adoption.

## Design system (Phase 1, Task-level foundation)

Tokens in `frontend/src/styles.css` `:root` — every component consumes these:

```css
--bg: #060B18;            /* deep navy page background, subtle radial gradient */
--card: #0F172A;          /* card surface */
--card-glass: rgba(15, 23, 42, 0.72);   /* glass panels: + backdrop-filter blur(12px) */
--accent: #4F7CFF;        /* electric blue: active states, links, sliders, glows */
--pos: #34D399;           /* emerald */
--neg: #F87171;           /* soft red */
--radius: 16px;
--border: rgba(148, 163, 184, 0.12);    /* 1px hairlines */
--shadow-1: 0 4px 24px rgba(0,0,0,0.35);
--glow-accent: 0 0 0 1px rgba(79,124,255,.35), 0 4px 20px rgba(79,124,255,.25);
```

- Typography: system stack; numbers ALWAYS `font-variant-numeric: tabular-nums`;
  labels 11px uppercase letterspaced; values 15–28px by prominence.
- Cards: `--card` fill, 16px radius, hairline border, top-edge 1px gradient highlight
  (`linear-gradient(90deg, transparent, rgba(79,124,255,.35), transparent)`), hover:
  translateY(-2px) + shadow deepen, 180ms ease.
- Motion: 150–250ms ease for hover/focus; 300ms for panel/chart transitions; a
  one-time fade-slide-in keyframe for cards on mount. No animation libraries.
- Buttons/pills: filled accent (primary), glass ghost (secondary); focus rings use
  `--glow-accent`.

## Builder page (Phase 1)

Layout top→bottom: header → stat cards → expiry picker → strike slider → payoff
centerpiece → sliders row → legs table (+toolbar) → recent trades. Grid tightened
(reduce empty space): max content width ~1280px, 16/24px spacing rhythm.

1. **Header:** ticker input (glass), large price + day-change stack (existing data),
   real-time market-status chip (reuses `market_status` from the dashboard snapshot
   endpoint: Open/Pre/After/Closed with pulsing dot).
2. **Stat cards** (replace `.ostats` strip): five cards — NET DEBIT (coin icon, blue),
   MAX LOSS (↘, red), MAX PROFIT (↗, emerald), PROBABILITY OF PROFIT (dice, blue;
   green value ≥50%), BREAKEVEN (→, neutral; "Above/Below $X (+/-%)"). Each: icon chip
   (28px rounded square, translucent color fill), large tabular number, small trend
   indicator = arrow + delta vs the PREVIOUS payoff refresh (session-local, resets on
   leg change; hidden when no previous value), hover lift. Values: existing
   `computeStats` + `chance_of_profit` — no new math.
3. **Expiry picker:** horizontal snap-scroll rail of day pills with floating month
   group labels (sticky within the rail), active pill = accent fill + glow, others
   glass. Auto-scrolls the active pill into view. Replaces ExpiryTabs' two-row layout;
   same props (`expirations`, `value`, `onChange`).
4. **Strike slider:** keep StrikeRuler mechanics (drag-snap, per-leg badges, per-expiry
   strikes) and add: hover tick highlight (accent), floating hover tooltip showing the
   strike value, 120ms badge position transitions, subtle gradient fade at rail edges.
5. **Payoff centerpiece:** height 380px. Existing gradient zones, breakeven line +
   label, spot marker, dotted strikes. NEW: (a) wheel-zoom over the chart adjusts
   `rangePct` (clamped 2–50, passive listener, smooth); (b) drag-pan when zoomed —
   a `panPct` offset shifts the window center (clamped so the window stays inside the
   curve; resets on re-quote); (c) 300ms animated transitions on data change
   (`isAnimationActive` with fixed duration); (d) a "Δ overlay" toggle button rendered
   in Phase 1 but disabled with a tooltip "Phase 2" until the greeks endpoint lands.
6. **Legs table:** modern rows on glass; BUY badge emerald / SELL badge red (filled
   translucent, 8px radius); Type column colored (Call = emerald text, Put = red text,
   Stock = blue text); rounded dropdown selects; qty stepper styling; per-row actions:
   **⧉ duplicate** (copies the leg, selects the copy), **✕ delete** (existing).
   Toolbar above the table: **+ Leg**, **Templates ▾**, **Add hedge**, and the Net
   debit/credit readout.
7. **Templates ▾** (menu; all built from the SELECTED expiry's per-expiry strikes,
   ATM = nearest to spot; every generated leg passes through the existing
   `invalidateQuote` flow):
   - Vertical Spread: BUY C@ATM, SELL C@ATM+2 steps.
   - Straddle: BUY C@ATM, BUY P@ATM.
   - Butterfly: BUY C@ATM−2, SELL 2×C@ATM, BUY C@ATM+2.
   - Iron Condor: SELL P@ATM−2, BUY P@ATM−4, SELL C@ATM+2, BUY C@ATM+4.
   - Covered Call: BUY 100×S (stock leg), SELL C@ATM+2.
   ("step" = index step in the expiry's strike list.) Templates REPLACE current legs
   (confirm via the existing msg banner pattern: first click arms, second within 5s
   applies — no modal).
8. **Add hedge:** computes net delta sign from current legs (Phase 1 heuristic:
   calls BUY→+, SELL→−; puts BUY→−, SELL→+; stock BUY→+, SELL→−; Phase 2 upgrades to
   real net delta): net +ve → BUY P at ~5% below spot (nearest listed strike),
   net −ve → BUY C at ~5% above; expiry = the front (earliest) leg expiry.
9. **Stock legs (`right: "S"`)** — backend `routers/builder.py` Leg model gains
   `right: Literal["C","P","S"]`; `options/combo.py` `payoff_at`/`combo_value` price a
   stock leg linearly (`value = S`, entry mid = spot); `/quote` prices stock legs at
   the facade spot (never theo-flagged); `payoff_curve` handles them via `payoff_at`.
   Builder monitor values them at spot. Qty for stock legs is shares (templates use
   100). Grep-gated: stock legs never appear in `app/broker/` paths.
10. **Sidebar:** inline SVG icon set (dashboard/grid, strategies/layers, builder/
    sliders, journal/book, api/plug); active item = accent left bar + icon glow +
    glass fill; sections collapsible ("TRADING": Dashboard/Strategies/Builder,
    "ANALYSIS": Journal/API) with chevron, state in localStorage; footer block:
    avatar circle (account initial), masked account number, live net-liq (existing
    `/api/account`, 60s refresh), market-status dot.
11. **Recent trades:** tracked-trades table → compact card list (underlying + legs
    count, entry date, P&L chip colored, status pill, Close button on open).

## Analytics (Phase 2)

12. **Greeks math** (`backend/app/options/blackscholes.py` additions): analytic
    `bs_greeks(S, K, T, iv, r, kind) -> {delta, gamma, theta, vega}` (theta per
    calendar day, vega per 1 vol point); stock kind `S` → delta 1, others 0.
    Unit-tested against textbook values (e.g. S=K=100, T=0.25, iv=0.2, r=0.05:
    call Δ≈0.5695, Γ≈0.0391, vega≈0.1955·100-scaled — exact expectations computed in
    the test with tolerances).
13. **`POST /api/builder/greeks`** body `{legs, spot, iv, r?}` → per-leg + net
    `{delta, gamma, theta, vega}` scaled ×100×qty (position greeks, dollars-per-point
    conventions documented in the response). Frontend Greeks panel: 4 tiles under the
    stat cards (Δ Γ Θ ν, net values, colored by sign), auto-refresh with the payoff.
14. **Greeks chart overlay:** server-computed — `/greeks` gains optional
    `curve: true`, returning `[{price, delta}]` (net position delta) across the same
    lo/hi window the payoff uses; PayoffChart renders it as a thin accent line on a
    secondary y-axis when the Δ toggle is on.
15. **IV smile** (`GET /api/builder/iv-smile?underlying&expiry&count=10`): for the
    near-ATM window (same slice as chain-quotes), take live mids (call side ≥ spot,
    put side < spot), invert BS by bisection (iv ∈ [0.01, 4.0], tol 1e-4, ≤60 iters;
    skip strikes with no mid or no root), return `[{strike, iv, side}]` + spot.
    Frontend: line+dot chart below the payoff (glass card, accent dots), "insufficient
    quotes" empty state. Round-trip test: price at known IV → invert → recover ±1e-3.

## Error handling

- Templates/hedge disabled until chain + spot are loaded; generated strikes clamp to
  the expiry's available list.
- Stock legs: no expiry (backend accepts empty string; UI shows "—"); excluded from
  expiry-derived logic (DATE scrubber uses option legs only; all-stock structures
  disable the scrubber).
- Zoom/pan clamps; double-click resets to default window.
- Greeks/IV endpoints degrade to nulls/[] on missing data; panels show em-dashes.

## Testing

- Backend: greeks values vs textbook; IV inversion round-trip; stock-leg payoff/quote
  (payoff linear, quote at spot, never theo); templates have NO backend surface.
- Frontend: `payoffStats` node tests keep passing (stock legs: netCalls unaffected —
  add one case: covered call → maxLoss finite via price-0 extrapolation, maxProfit
  bounded); build green per task.
- Visual: screenshot after the design-system task, the Builder-rebuild task, and each
  Phase-2 chart — compared against this spec's intent by the user.

## Non-goals

- No frontend framework/library adoption (no Tailwind, no framer-motion).
- No real-time streaming re-render loop beyond existing polling cadences.
- No order-side support for stock legs (Builder is paper-only; unchanged invariant).
- No IV surface (3D) — smile per selected expiry only.
- No user-configurable themes; one premium dark theme.
