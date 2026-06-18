# Migrate Trading System to IBKR Gateway — Design Spec

Date: 2026-06-18
Status: Approved — implement.

## Goal
Replace the dual Tastytrade (live + execution) / Massive (historical) backend with a single
**Interactive Brokers** backend via **IB Gateway** (paper trading). Live market data, paper
execution, accounts, and positions all come from IBKR. Reuse the existing seams so callers
(strategy worker, kill switch, routers, dashboard) are largely untouched.

## Decisions (locked)
1. **Library:** `ib_async` (2.1.0) — asyncio-native, pure-Python wheel, resolves on the
   Python 3.14 venv (deps: aeventkit, nest_asyncio, tzdata; no greenlet/compiled code).
2. **Backtest data:** keep the existing `data_cache/*.json` (2yr, real) read via
   `history_cache`. IBKR supplies live/forward data only. Bulk intraday history is NOT
   re-pulled from IBKR (pacing-limited, shallow).
3. **Market data:** real-time (`reqMarketDataType(1)`) — account has live subscriptions.
4. **Cutover:** hard — delete all Tastytrade and Massive code.

## Architecture

### New package `app/ibkr/`
- **`gateway.py`** — singleton connection manager around `ib_async.IB()`. Async
  `connect()`/`get_ib()`/`disconnect()`; auto-reconnect; tracks connection state. Connects
  to `ibkr_host:ibkr_port` with `ibkr_client_id`, sets `reqMarketDataType`. This *replaces
  OAuth*: "authenticated" → "Gateway connected & API reachable."
- **`market.py`** — `get_quotes(symbols)` via `reqMktData`/tickers, normalized to the
  existing quote dict shape. Live streaming for `services/market_stream.py` rebuilt on
  ib_async ticker events.
- **`account.py`** — `get_accounts()` (managedAccounts), `get_positions(account)` →
  normalized `{symbol, quantity, direction, instrument_type}` matching what the kill switch
  and dashboard already consume.
- **`contracts.py`** — `stock(symbol)` helper (`Stock(symbol, "SMART", "USD")`) + qualify.

### Config (`app/config.py`)
- **Remove:** every `tastytrade_*` (oauth client/secret/refresh, is_test) and `massive_*`.
- **Add:** `ibkr_host` (default `127.0.0.1`), `ibkr_port` (default `4002` = paper Gateway),
  `ibkr_client_id` (default `1`), `ibkr_account` (blank → first managed paper account),
  `ibkr_market_data_type` (default `1`).
- **Keep:** `ENABLE_LIVE_ORDERS` — off = simulate (log only); on = transmit to the IBKR
  **paper** account. (Paper is itself safe; the gate stays for pure-sim testing.)

### Historical provider (`app/providers/`)
- New `ibkr_historical.py` implementing `HistoricalDataProvider.get_bars` via
  `reqHistoricalDataAsync`, normalized to `{time(ms,UTC), open, high, low, close, volume,
  symbol}`. Used for live-forward refresh / scanner only (modest durations).
- `providers/__init__.py` factory returns the IBKR provider. **Delete `massive.py`.**
- Backtest runners keep loading cached files (`source="massive"` namespace stays valid — it's
  real data; new IBKR fetches save under `source="ibkr"`).

### Execution (`app/engine/execution.py` — rewrite, identical public API)
Same function names/signatures so no caller changes:
- `submit_equity_order(symbol, intent, quantity)` → `MarketOrder(BUY|SELL)` on a qualified
  `Stock`. Intent map: enter_long/exit_short→BUY, exit_long/enter_short→SELL.
- `submit_bracket_order(symbol, side, quantity, entry, stop, target)` → `ib.bracketOrder()`
  (native parent market + take-profit limit + stop child), transmit all three.
- `cancel_symbol_orders(symbol)` → cancel matching `ib.openTrades()`.
- `poll_bracket_fill(symbol, ref)` → inspect `ib.positions()`/trade fills; return
  `{price, ms, reason}` or None, same contract as today.
- `flatten_all()` → cancel all open orders, then market-close every position; **raise if any
  exposure remains** (preserve kill-switch honesty / `fully_flat`).
- `ENABLE_LIVE_ORDERS` gate preserved exactly.

### Auth / session (`app/auth/`)
- Replace OAuth `session_manager` with IBKR connection state (`is_connected()` backed by
  `gateway`). Update the kill switch and any `is_authenticated()` callers to the new check.

### Frontend
- API/Connection tab: replace Tastytrade OAuth fields with IBKR Gateway config
  (host/port/clientId/account) + a live connection-status indicator. Dashboard auth badge →
  "Connected to IBKR / Disconnected." No secrets to the browser (supersedes the old
  api-tab-auth-ux tradeoff).

### Removal
- Delete `app/tastytrade/`, `app/providers/massive.py`, `app/providers/tastytrade_live.py`,
  OAuth pieces of `app/auth/`, Tastytrade/Massive settings, and the `tastytrade` + `greenlet`
  dependencies. Update/remove tests importing them.

## Testing
- **TDD the pure logic** by stubbing the `IB` object: intent→action mapping, bracket
  construction, and bar/quote/position normalization. These need no Gateway.
- **`scripts/connect_smoke.py`** — user-run: connects to the Gateway, prints accounts, a live
  quote, and (with `ENABLE_LIVE_ORDERS=1`) a 1-share paper bracket round-trip. This validates
  the live path, which cannot be exercised from the dev box (no Gateway reachable here).
- Existing strategy/backtest tests must stay green (they don't touch the broker).

## Out of scope
- Re-pulling bulk historical from IBKR; options/futures; multi-account; real-money trading
  (paper only). Strategy logic is unchanged.

## Risks
- Requires IB Gateway running + logged into the paper account for any live path; agent cannot
  reach it — live validation is via the smoke script. `clientId` collisions if TWS/Gateway
  already has a client on that id (configurable). Pacing limits make IBKR historical unsuitable
  for bulk backfill (hence decision 2).

## Notes
Repo not under git; spec on disk, not committed.
