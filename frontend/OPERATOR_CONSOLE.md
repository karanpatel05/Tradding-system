# Operator console frontend

The main application now uses the approved dark navigation rail and light operator workspace. Run `npm run dev` from `frontend` and open http://localhost:5173. This is the existing API-backed application; `redesign.html` remains the separate sample-data concept preview.

## Implemented

- Persistent navigation, feed status, emergency stop, recovery banners, and source/routing indicators.
- Portfolio overview with compact market board, account metrics, app-owned positions, strategy activity, allocation budgets, and system health.
- Strategy table and existing strategy detail, allocation, activation, and backtest workflows.
- Options builder with a primary analysis workspace and separate trade setup panel; templates, stock/option legs, quote/payoff, Greeks, IV smile, and tracked trades retain their existing API behavior.
- Journal calendar and session detail layout, keyboard-accessible date buttons, local calendar date keys, and distinct loading/error states.
- Connections with separate market data, Alpaca paper, access token, and order-routing sections.
- Shared light chart and component styling, visible keyboard focus, and responsive layouts.

## Data and execution

Tastytrade is market data; Alpaca paper is execution and account balances. No sample figures or mock charts were introduced into the main application. Allocation bars describe strategy budgets, not risk. Missing account values show dashes and account request failures are explained. Disconnected snapshots are identified as stale. The existing confirmation and backend authorization checks still apply to order routing, activation, resume, and emergency stop.

## Verification

`npm run build` and the existing `node --test src/components/builder/*.test.mjs` suites pass. Browser checks covered all five screens, strategy detail navigation, tracked trades, journal month changes and saved session details. Each main screen was checked at 390px width without horizontal page overflow. No order submissions, activation, halt, resume, or routing changes were made during verification.

Authenticated account access was subsequently verified with HTTP 200 after configuring the app token and restarting the backend. The token remains in the ignored local `backend/.env` file and is entered through Connections; it is never bundled with the frontend. Order routing remained simulated. Screenshots from the running account and authentication setup are kept local and ignored by Git. The production build reports a non-blocking JavaScript bundle-size warning.
