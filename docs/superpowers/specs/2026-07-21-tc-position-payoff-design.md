# Triple Calendar open-position payoff — design

**Date:** 2026-07-21
**Status:** approved (design), pending spec review → implementation plan
**Branch:** `tc-position-payoff`

## Problem

The Options Strategy Builder renders a payoff diagram for *hypothetical/paper* structures
(`PayoffChart.jsx`, backed by `/api/builder/payoff` + `/greeks`). The **live Triple Calendar
position page** (`OptionsStrategyDetail.jsx`) shows legs, DTE, live P&L, and target/stop
progress — but **no payoff diagram**. A calendar spread's shape is not obvious from the legs,
so the user wants the same payoff view (plus supporting stats) for the actual open position.

## Goals

For each **open Triple Calendar position** (per ticker), show on `OptionsStrategyDetail`:

1. A payoff curve (P&L vs underlying price) evaluated at the near (front-month) expiry.
2. Breakevens and max profit / max loss over the plotted window.
3. A current-underlying marker and the live mark-to-market P&L.
4. Chance of profit (lognormal, same model as the Builder).
5. A net-greeks panel (δ / γ / θ / vega) for the position.

## Non-goals (v1)

- Equity/stock positions (a stock payoff is a straight line — no value here).
- A "today vs expiry" dual-curve overlay (single near-expiry curve for v1).
- IV term structure / per-expiry vol (single-IV approximation, exactly as the Builder does).
- Any new order actions. This is read-only visualization.

## Architecture — Approach C (dedicated module + endpoint)

Chosen over: (A) inlining fields into the `options-position` response — bloats a 733-line
router and recomputes on every 15s poll; (B) frontend calling the Builder endpoints —
duplicates leg-construction / IV / contracts-scaling logic in untestable JS.

### New backend module: `backend/app/options/position_payoff.py`

Pure functions (no DB, no network) so they're unit-testable in isolation. Reuses the
existing math in `app/options/combo.py` (`payoff_at`, `payoff_curve`, `curve_stats`,
`chance_of_profit`) and `app/options/blackscholes.py` (`bs_greeks`).

```
legs_from_calendars(calendars) -> list[dict]
    # each {strike, sell, buy} -> two CALL legs:
    #   {action:"SELL", right:"C", strike, expiry: sell, qty:1}
    #   {action:"BUY",  right:"C", strike, expiry: buy,  qty:1}
    # (TC is all calls: near=SELL, far=BUY — see triple_calendar/live.py, strategy.py)

fit_iv(legs, spot, net_debit, live_value, *, default=0.25) -> tuple[float, str]
    # Solve a single IV so payoff_at(legs, spot, today, iv) == live_value (anchors the
    # model to the live mark). Bisection over [0.01, 3.0]. Returns (iv, source) where
    # source ∈ {"fit", "assumed"}. Falls back to `default`/"assumed" when spot or
    # live_value is None, or the fit doesn't bracket a root.

build_position_payoff(record, contracts, *, r=0.04, points=61) -> dict
    # Orchestrates: legs -> eval_date = min(sell expiries) -> iv (fit) ->
    # window from strike span -> payoff_curve -> curve_stats -> chance_of_profit ->
    # net greeks. Scales dollar quantities (curve pnl, max_profit, max_loss) by contracts.
```

**Eval date:** the nearest `sell` expiry (front-month expiration — the standard calendar
payoff view; longer-dated legs are BS-valued there, which `payoff_at` already does via
`T = (expiry - eval_date).days / 365`).

**Greeks:** net position greeks from `bs_greeks` per leg at the current spot, summed with
sign (SELL = −, BUY = +), ×100 per contract-share, ×`contracts`.

### New endpoint: `GET /api/strategies/{key}/options-position/payoff?ticker=<T>`

- In `app/routers/strategies.py`. Read-only, **ungated** (consistent with the other
  `options-position` reads; it exposes no secrets and places no orders).
- Loads the open `OptionPositionRecord` for `(key, ticker, status="open")`, resolves the
  contracts count exactly as `options_position` does, calls `build_position_payoff`.
- **Flat / no open position** → `{"has_position": false}` (200, not 404) so the frontend
  renders a placeholder without treating it as an error.
- `_require_ready(key)` guard, same as the sibling routes.

### Response shape

```json
{
  "has_position": true,
  "ticker": "QQQ",
  "eval_date": "2026-08-15",
  "spot": 480.12,
  "iv": 0.187,
  "iv_source": "fit",
  "contracts": 2,
  "net_debit": 4.15,
  "live_value": 4.62,
  "current_pnl": 94.0,
  "curve": [{"price": 460.0, "pnl": -180.5}, ...],
  "strikes": [470.0, 480.0, 490.0],
  "breakevens": [472.3, 488.9],
  "max_profit": 512.0,
  "max_loss": -190.0,
  "chance_of_profit": 0.41,
  "greeks": {"delta": 3.1, "gamma": 0.8, "theta": 12.4, "vega": -45.2}
}
```

## Frontend

New **Payoff** card in `OptionsStrategyDetail.jsx`, one per ticker with `has_position: true`:

- `api.optionsPositionPayoff(key, ticker)` added to `api.js` (sends the app token via the
  shared header like every request; endpoint is open so it works without a token too).
- Renders the existing `<PayoffChart curve breakevens strikes spot />` (no new chart code).
- A stat row beneath: breakevens · max profit · max loss · chance of profit.
- A net-greeks panel (δ/γ/θ/vega), reusing Builder greeks styling.
- An IV badge: `IV 18.7% (live-fit)` or `IV 25% (assumed)` so the model basis is explicit.
- Polls on the existing 15s `loadOp` cadence (fetch payoff alongside the position).

## Testing

`backend/tests/test_position_payoff.py` (pytest, offline — no DB/quotes):

- `legs_from_calendars` produces the 6 expected SELL-near / BUY-far call legs.
- `fit_iv` recovers an IV that reprices `payoff_at(spot, today)` to `live_value` (round-trip),
  and returns `("assumed", default)` when `live_value`/`spot` is None.
- `build_position_payoff`: curve is monotone-tent-shaped around the strikes with a peak near
  the middle strike; breakevens bracket the spot; `max_profit`/`max_loss` scale linearly with
  `contracts`; `current_pnl == (live_value - net_debit) * 100 * contracts`.
- Endpoint test (TestClient): open record → 200 with `has_position: true` and a non-empty
  curve; no record → `{"has_position": false}`.

## Known limitations (documented, not bugs)

- **Max loss reflects the plotted price window**, not the theoretical tails. A calendar's
  true max loss ≈ the net debit as the underlying runs far from the strikes; the UI labels the
  figure as the max loss *in the shown range* rather than overstating it.
- **Single-IV model**, no term structure between the near and far expiries — the same
  approximation the Builder already ships. The live-fit anchors it to the real mark, which is
  the most defensible single-number choice.

## Files touched

- `backend/app/options/position_payoff.py` (new)
- `backend/app/routers/strategies.py` (new endpoint + route)
- `backend/tests/test_position_payoff.py` (new)
- `frontend/src/api.js` (new client method)
- `frontend/src/components/OptionsStrategyDetail.jsx` (Payoff card)
