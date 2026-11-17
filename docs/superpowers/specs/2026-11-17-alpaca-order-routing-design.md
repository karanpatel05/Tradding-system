# Alpaca order routing (paper-locked, shared account) + Triple Calendar removal — design

Date: 2026-11-17 · Status: draft for approval

## Intent

The user runs a separate trading system on this computer that trades the same
Tastytrade account. This app should keep **Tastytrade for market data only** and send
**its own orders to Alpaca**. Until the user supplies live Alpaca keys, everything is
**Alpaca paper only**. The Alpaca paper account is **shared**: it may hold positions and
orders the app didn't create (manual trades, other tools). The Triple Calendar strategy is
removed entirely. No strategy is being added now — this work builds the order plumbing
and leaves the strategy slots empty.

## Goals

1. Every order path (equity, bracket, multi-leg options, cancel, fill check, kill-switch
   flatten) talks to **Alpaca** instead of Tastytrade.
2. **Paper lock:** Alpaca writes (submit, cancel, close) only ever go to
   `https://paper-api.alpaca.markets`. Any other `APCA_API_BASE_URL` makes them refuse.
3. **Ownership:** the app only cancels, closes, reconciles and reports on activity **it
   created**, identified by a tagged `client_order_id`. Everything else in the account is
   left alone.
4. Startup reconciliation compares the app's **transmitted** trades with what Alpaca says
   the app's own orders did. Simulated trades (gate OFF) never trip safe mode.
5. Tastytrade remains the data source: quotes, option chains, history, market overview,
   options Builder valuation, and the probe used by the auth gate.
6. Triple Calendar is gone from the Strategies section, never auto-resumes, and its code
   and tests are deleted; its two stale "open" rows are closed out.

## Non-goals

- Adding or re-enabling any strategy (EMA scalper/crossover stay unregistered).
- Live (real-money) Alpaca trading — unlocking is a later, deliberate code change.
- A broker switch / keeping Tastytrade order routing (approach B was declined).
- Changing the options Builder (stays paper-tracked; never places orders).
- Managing or reporting on positions the app didn't open.

## Anti-goals (failure even if it "works")

- Any code path that can transmit an order to Tastytrade.
- Any code path that can write to a non-paper Alpaca URL.
- **The kill switch closing or cancelling anything the app didn't open** (no
  `DELETE /v2/positions`, no `DELETE /v2/orders` without a filter).
- Manual/other positions in the Alpaca account, or the other system's Tastytrade
  positions, tripping this app's safe mode.
- Alpaca keys or full account numbers reaching the frontend or logs.

## Constraints

- Alpaca credentials: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `APCA_API_BASE_URL`
  (default paper) in `backend/.env` — already in `app/config.py`.
- HTTP via `httpx` (already a dependency; see `app/services/alpaca_status.py`). No new SDK.
- Alpaca positions are **aggregated per symbol** across all activity in the account, so
  position size alone cannot tell app-owned shares from manual ones. App ownership must
  come from the app's own tagged orders and their fills.
- Alpaca multi-leg (`order_class: "mleg"`) allows **at most 4 legs**; options orders are
  `time_in_force: "day"`. Alpaca option symbols are OCC **without padding**
  (`QQQ261117C00660000`) vs Tastytrade's padded form (`QQQ   261117C00660000`).
- Keep the return shapes of the `app.engine.execution` functions (consumed by
  `app/engine/worker.py` and the kill switch).
- `ENABLE_LIVE_ORDERS` keeps its meaning and type-LIVE UX: OFF = internal simulated fills
  exactly as today (no network order call); ON = transmit to **Alpaca paper**.
- `client_order_id` max length is 48 characters.

## Approaches considered

- **A. Replace Tastytrade order routing with Alpaca — chosen.** One Alpaca-backed order
  module; broker truth from Alpaca scoped to the app's own orders; Tastytrade for data.
- **B. Switchable broker (ALPACA | TASTYTRADE).** Declined: ~2x order code and tests for a
  route the user doesn't intend to use (that Tastytrade account belongs to the other system).

## Design (decided)

### Ownership tagging
Every order the app submits carries `client_order_id = "tsys-" + <order-store ref>`
(≤48 chars). For brackets/mleg the parent carries the tag; Alpaca children are found via
the parent (`nested=true`). "App-owned" = orders whose `client_order_id` starts with
`tsys-`. Each submit records an order-store intent **before** transmit (as equity submits
do today), so the ref exists even if the HTTP call fails mid-flight.

### Order module
Replace `app/broker/orders.py`'s Tastytrade internals with Alpaca, keeping the functions
and return shapes re-exported by `app/engine/execution.py`:

| Function | Alpaca behavior |
|---|---|
| `submit_equity_order(symbol, intent, qty)` | `POST /v2/orders` market, `day`, tagged. |
| `submit_bracket_order(symbol, side, qty, entry, stop, target)` | `order_class: "bracket"`, market entry, `take_profit.limit_price`, `stop_loss.stop_price`, `gtc`, tagged. Returns `parent_order_id`. |
| `submit_combo_order(underlying, legs, net_price, action, quantity)` | `order_class: "mleg"`, ≤4 legs (more → `transmitted: False` with a clear detail; never split), per-leg `position_intent` open/close based on the **app's own** open legs, limit at net price if given else market, `day`, tagged. (No caller after TC removal; kept as plumbing.) |
| `cancel_symbol_orders(symbol)` | cancel **app-owned** open orders for that symbol (incl. bracket children); wait until they're gone before returning, because Alpaca reserves shares for open bracket legs and the following exit would reject. |
| `poll_bracket_fill(symbol, ref)` | Decide from the **app's bracket order**, not the account position (shared account): `None` while the entry is filled and no exit child has filled; else `{price, ms, reason}` from the filled child (stop vs take-profit). Same contract as today. |
| `flatten_all()` | Live: cancel app-owned open orders, then for each symbol with an app-owned net position submit a tagged market close for **that quantity only**, capped at the account's actual position in the same direction. Poll a short window for fills (orders queue outside regular hours) and raise if anything app-owned is left (kill switch reports `fully_flat=False`). Never touches untagged positions/orders. Paper (gate OFF): unchanged would-close + raise. |

Transmit-path order of checks: quantity → safe mode → `ENABLE_LIVE_ORDERS` → **paper
lock** → order-store intent → submit. Alpaca 4xx rejections return `transmitted: False`
with Alpaca's message (Alpaca has no dry-run; no separate validation call).

### App-owned position ledger
A helper computes the app's net position per symbol from **Alpaca's own record of the
app's tagged orders** (`GET /v2/orders?status=all` filtered to `tsys-` client ids, with
nested children, summing signed `filled_qty`). This is the "broker truth" for
reconciliation, the kill switch, combo `position_intent`, and the dashboard. If the
account's actual position for a symbol is smaller than the app-owned net (someone closed
app shares by hand), that is a discrepancy.

### Reconciliation (startup and on demand)
- Add an additive nullable/boolean column `TradeRecord.transmitted` (default false;
  added via the existing lightweight `ALTER TABLE` pattern in `app/db/base.py`).
  Workers set it true when the order result has `transmitted: True`.
- `local_open_positions()` counts only `status == "open" AND transmitted`.
- Compare that with the app-owned ledger (above), plus the "account position smaller than
  app-owned net" check. Mismatch → safe mode (unchanged behavior). Untagged Alpaca
  positions and Tastytrade positions are ignored entirely.
- The current safe-mode flag (tripped by the other system's Tastytrade QQQ legs) stays on
  after the switch — recovery only trips, never clears. Rollout step: after the switch,
  Resume once from the UI (with the app token) so it re-reconciles against Alpaca.

### Paper lock
One helper returns the trading base URL only if its host is exactly
`paper-api.alpaca.markets`; otherwise every Alpaca write returns
`{"transmitted": False, "detail": "blocked: Alpaca live endpoint locked (paper only)"}`
and logs an error. Read-only calls may use the configured URL.

### Broker views from Alpaca
`app/broker/account.py` becomes Alpaca-backed with the same names/shapes:
- `balances()` — account-level numbers from `/v2/account` (equity→net_liquidation, cash,
  buying_power, …, masked account). This is the whole (shared) account; the UI labels it so.
- `positions()` — **app-owned** positions only (from the ledger, with avg price / mark from
  `/v2/positions` for those symbols), as `NormalizedPosition` (`"Equity"` /
  `"Equity Option"`). Option symbols normalized to unpadded OCC on both sides.
- `live_orders()` / `cancel_order(id)` — app-owned orders only; cancel refuses untagged ids.
Consumers (dashboard REST, WebSocket snapshot, reconciliation, kill switch) stay unchanged.
Dashboard `/positions`, `/account` and the WebSocket account snapshot are gated on Alpaca
readiness, not the Tastytrade probe.

### Tastytrade = data only
Keep the Tastytrade session/streamer/probe/facade. Remove Tastytrade order code
(`place_order`, complex orders, `build_otoco` SDK use, `Account` order/cancel calls) and
the Tastytrade account/positions reads used for broker truth.

### UI
- Status chips: `Data: Tastytrade` (unchanged) and `Broker: Alpaca paper · <state>`.
- Orders chip / Order Routing card: OFF = **Simulated**, ON = **Alpaca paper**; arming
  copy says orders go to Alpaca's paper account.
- Dashboard: Account card = Alpaca paper account balances, labelled "shared account";
  Open Positions = app-owned positions only.
- API tab: "Broker — Tastytrade" card becomes "Market data — Tastytrade"; the Alpaca card
  is the broker card. No credential inputs (unchanged).

### Triple Calendar removal
- Unregister it (`registry.py`); `strategy_2` reverts to a Coming Soon placeholder.
  `seed_strategy_state` also resets a non-ready slot's `status` to `idle`, so the old
  "scanning" row neither resumes nor shows as running.
- Delete `app/strategies/triple_calendar/`, TC-only endpoints/fields in
  `app/routers/strategies.py`, `tc_*` settings, and the 24 `tests/test_tc_*.py`.
- Move `LivePriceSource` (imported by `app/options/chain.py`) into `app/options/` so the
  Builder keeps working.
- Frontend: remove `OptionsStrategyDetail.jsx` + its routing in `StrategyDetail.jsx`, TC
  calls in `api.js`, the TC CSS block, and TC rows in `redesign/main.jsx` mock data.
- Keep DB tables/columns (`tc_tickers`, `option_positions`); no destructive migration.
- **One-time data fix:** close the two stale TC rows in `option_positions` (ids 59 QQQ,
  69 SPY; `strategy_2`, paper, "open" since 2026-08-13) — status closed, P&L null, closed
  now. Idempotent (only rows still open for `strategy_2`). The Journal shows them as
  finished history.

### Tests
- Rewrite broker/execution tests (`test_broker_*`, `test_combo_*`, `test_options_combo`,
  `test_execution_ibkr`, `test_kill_flatten_ledger`, `test_submit_idempotent`,
  `test_worker_transmit_guard`, `test_live_orders_switch`, recon/recovery tests) against
  Alpaca with `httpx.MockTransport` — no network in tests.
- New: paper-lock refusal for submit/cancel/close; >4-leg refusal; OCC normalization;
  **untagged positions/orders are never cancelled, closed, or counted**; flatten closes
  only the app-owned quantity when a manual position shares the symbol; simulated
  (non-transmitted) open trades don't trip safe mode; no Tastytrade order API is reachable.
- Manual check with `ENABLE_LIVE_ORDERS=true` against the paper account: one 1-share
  equity order, cancel/close it, run flatten, confirm untagged holdings untouched, switch OFF.

## Calls made by the author (correct if wrong)

- Tag prefix `tsys-` + order-store ref as the ownership marker.
- Bracket and combo submits also record order-store intents (today only equity does).
- Flatten verifies by polling for a short window and reports not-flat honestly after it.
- Exits wait for app-owned bracket-leg cancels to clear before sending the close.
- Account balances shown are account-wide (shared), labelled as such; positions are app-only.

## Left to the builder

- Module split (e.g. `app/broker/alpaca_client.py` for base URL, headers, errors, paging).
- Field mapping for balances not 1:1 with the old shape (use `None`).
- Ledger query paging/caching and how far back to look (at least all orders with
  app-owned open quantity).
- Order of work, provided the paper lock, ownership filter and "no Tastytrade transmit"
  guarantees land before any code can transmit.
- Commit granularity.
