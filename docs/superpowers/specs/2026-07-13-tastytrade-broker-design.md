# Tastytrade as the sole broker (IBKR removed) — design

**Date:** 2026-07-13
**Status:** Approved (brainstorm) → implementation

## Goal

Remove IBKR entirely. Tastytrade — already the primary market-data source
(`2026-07-09-tastytrade-data-design.md`) — becomes the ONLY broker: order execution,
accounts/positions/balances, reconciliation, kill switch, connection status. End state:
one broker, one data source, zero local Gateway processes, zero daily logins.

## Decisions (from brainstorm)

- **Orders route to the user's REAL Tastytrade account**, gated by the existing
  `ENABLE_LIVE_ORDERS` flag, **default OFF**: strategies run full logic and record
  **simulated fills at the facade mid** (sanity-guarded) as paper trades. Flipping the
  flag makes the *identical call path* place real orders. No Tastytrade cert/sandbox.
- **Delete all IBKR code** (git history is the archive): `app/ibkr/*`,
  `engine/ibkr_orders.py`, `providers/ibkr_historical.py`, the Gateway conn-state
  machine/reconnect supervisor, IBKR settings, the `ib_async` dependency, the facade's
  IBKR fallback paths, and the ApiTab IBKR card.
- **Approach: strangler swap.** Build the Tastytrade implementation behind the exact
  call signatures the engine already uses (`submit_order`, `submit_combo_order`,
  `flatten_all`, positions/balances/status), swap imports, then delete IBKR in a final
  sweep. The system keeps working at every commit.

## Current IBKR footprint (26 modules touch it)

auth/{gate,manager,routes}, engine/{execution,ibkr_orders,reconciliation,recovery,worker},
ibkr/{gateway,market,contracts,account,options_data}, marketdata/facade, options/{chain,monitor},
providers/{base,ibkr_historical}, routers/{dashboard,strategies,system,ws},
services/market_stream, strategies/triple_calendar/strategy_class, config, main.

## Architecture

```
strategies / engine workers / routers            (call signatures unchanged)
        │
        ▼
app/broker/  (new)  ── orders.py: submit_order / submit_combo_order / flatten_all
        │            ── account.py: balances / positions / order status / cancel
        │            gate: ENABLE_LIVE_ORDERS off → SimulatedFill(mid) ; on → real order
        ▼
app/tastytrade/{session,streamer}  +  app/marketdata/facade  (TT → None; no IBKR)
```

### `app/broker/account.py`
- `get_tt_account() -> Account | None`: cached; obtained via `Account.get(session)` and
  the **real object is threaded everywhere** (SDK gotcha: `Account` has ~12 required
  fields and cannot be reconstructed from `account_number`). Cache reset with
  `reset_tt_session()` (creds change).
- `balances() -> dict` (net-liq, cash, buying power — the dashboard account panel shape),
  `positions() -> list[dict]` (symbol, instrument-type, qty, avg price, mark),
  `live_orders() -> list`, `cancel_order(id)`, `order_status(id)`.
- All read calls degrade to None/[] on failure (same never-raise contract as the facade).

### `app/broker/orders.py`
- `submit_order(symbol, qty, action, order_type="MKT", limit_price=None) -> Fill | None`
  — equities (EMA scalper). TT mapping: `Equity.get(session, symbol)` →
  `instrument.build_leg(Decimal(qty), OrderAction.BUY_TO_OPEN/…)` → `NewOrder(
  time_in_force=Day, order_type=Market/Limit, legs=[leg], price=…)` →
  `account.place_order(session, order, dry_run=False)`.
- `submit_combo_order(underlying, legs, **kw) -> Fill | None` — multi-leg options
  (Triple Calendar). Option instruments fetched by OCC symbol (built from
  `(underlying, expiry, strike, right)` — the chain's `by_key` already maps these);
  one `NewOrder` with all legs; net price = combo limit at the sanity-guarded mid
  (marketable limit), TIF Day.
- **Action mapping is the money-losing risk** and is unit-tested exhaustively:
  BUY→`BUY_TO_OPEN`/`BUY_TO_CLOSE`, SELL→`SELL_TO_OPEN`/`SELL_TO_CLOSE`, chosen by
  whether the account currently holds the instrument (positions lookup); qty sign
  conventions asserted per SDK requirements.
- **Gate:** `enable_live_orders=False` → no network order call; returns
  `SimulatedFill(price=facade mid, time=now, paper=True)`. `True` → `dry_run=True`
  validation first (Tastytrade prices buying-power effect without placing; an error
  rejects locally with the reason journaled), then the real placement.
- Every transmit/simulate is journaled (category="trade") with the gate state.

### Kill switch (`flatten_all`)
Cancel all live TT orders, then close every TT position with a market order
(reverse-direction, TO_CLOSE). Works identically with the gate off (simulated closes
against internal records). Same `/api/kill` endpoint and UI button.

### Reconciliation / recovery / SAFE_MODE
- Same semantics, new source: DB open records vs `broker.positions()`. Mismatch →
  SAFE_MODE (manual resume), exactly as today.
- The Gateway conn-state machine (`ConnState`, reconnect supervisor, settle delays)
  is deleted. "Broker ready" becomes: TT probe ok (`_probe_tt`, already built).
  `session_manager.is_authenticated()` → broker probe-backed equivalent; `auth/routes`
  `/status` reports {connected: probe ok, detail}.
- TC monitor-only mode: triggers when the probe fails (genuine TT outage) instead of
  Gateway state; market-closed pause unchanged.

### Dashboard / status
- Account panel + positions from `broker.balances()/positions()`.
- `services/market_stream` (WS push): drop the ib_async ticker loop; read the DXLink
  streamer's quote/trade cache for the overview symbols (subscribe once on start).
- Broker chip = probe (Connected ✓ / Rejected / down); Data chip unchanged.
- ApiTab: IBKR connection card replaced by a Tastytrade broker card (account number
  masked, net-liq, probe badge — creds card already exists).

### Facade after IBKR removal
- `_race` keeps its shape but the `ib_call` argument is dropped everywhere → op order is
  TT → None. Sanity guard, per-instrument source tracking, and `data_status()` stay
  (sources become {"tastytrade"} — the Data chip's "fallback" state disappears).
- `chain_strikes_for` keeps per-expiry behavior (no more union fallback).

### Config
- Remove: `ibkr_host/port/client_id/account/market_data_type/reconnect_interval/
  account_settle_seconds`, `data_primary` (no second source to prefer).
- Keep: `enable_live_orders` (default False — comment updated to Tastytrade), TT creds,
  sanity-guard settings.
- `tt_account_number: str = ""` (blank → first account from `Account.get_all`).

### Deletions (final sweep, after the swap is green)
`app/ibkr/` (all), `engine/ibkr_orders.py`, `providers/ibkr_historical.py` (+ factory
returns TT provider unconditionally), IBKR branches in facade/options/chain/monitor/
market_stream/routers/auth, `ib_async` from requirements, IBKR settings, ApiTab IBKR
card, ConnectionBanner Gateway wording. Grep gates in the final task:
`grep -r "ib_async\|app.ibkr\|get_gateway\|ConnState" backend/app` must be empty.

## Safety invariants (asserted by tests)

1. `enable_live_orders` defaults to **False**; with it False, NO code path constructs a
   network order call (test: fake `place_order` that raises if touched).
2. Builder still transmits nothing ever (no broker import under `app/options/` or
   `routers/builder.py`).
3. Kill switch cancels+closes in live mode and closes records in paper mode.
4. Simulated fills use sanity-guarded mids only (a rejected mid → no fill, retry next
   tick — never a garbage entry price).
5. Reconciliation mismatch still forces SAFE_MODE before any new order.

## Testing

- **Unit (fakes, no network):** action/qty mapping per instrument+direction (incl.
  TO_OPEN vs TO_CLOSE via positions lookup); combo leg building from TC leg dicts; gate
  OFF → simulated fill at mid + zero order-API calls; gate ON path calls dry_run before
  place; flatten_all ordering (cancel → close); reconciliation mismatch → SAFE_MODE.
- **Regression:** entire existing suite (TC live standalone tests keep faking the
  executor — unchanged); full pytest must stay green at every task.
- **Live smoke (safe):** `dry_run=True` order validation against the real account —
  proves auth, instrument mapping, and buying-power plumbing end-to-end while placing
  nothing. Balances/positions read. Kill switch exercised in paper mode only.
- **Manual gate:** flipping `ENABLE_LIVE_ORDERS` stays a deliberate, user-only action;
  nothing in this project flips it.

## Non-goals

- No Tastytrade cert/sandbox environment.
- No new order types (market/limit day orders only — what the strategies use today).
- No changes to strategy logic, Builder features, or the market-data facade interface.
- No multi-account support (single account, `tt_account_number` optional pin).

## Risks / notes

- Real-account credentials now gate order placement: the probe badge (Connected ✓)
  becomes the broker health signal; a rejected grant means no orders (and monitor-only
  TC), surfaced in UI + journal.
- The pre-IBKR system placed Tastytrade orders with this same SDK family — order-side
  competence exists in git history (pre-2026-06-18) for reference, but the SDK is now
  v12 OAuth; mappings are re-derived from the current SDK, not copied.
- Phased plan (~14–16 tasks): broker account/reads → orders+gate → kill switch →
  reconciliation/status swap → market_stream/dashboard → router/auth swap → deletion
  sweep → full verification. System green at every commit.
