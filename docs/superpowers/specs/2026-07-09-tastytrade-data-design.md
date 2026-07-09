# Tastytrade as primary market-data source — design

**Date:** 2026-07-09
**Status:** Approved (brainstorm) → implementation

## Goal

Replace IBKR as the market-data source with the **Tastytrade API** (REST + DXLink streaming).
IBKR Gateway remains for **order execution only**, plus a data **fallback** while it happens to
be connected. Motivation: the Gateway logs out daily (disconnect warnings every day 7/1–7/9,
`reconnect_failed` on three of those days), and IBKR option mids are inconsistent — measured
2026-07-09 just after the close: QQQ 21-DTE mids 100% missing, 43-DTE 86% missing, while
1-DTE was 0% missing; on 2026-06-30 (the only live TC trading day) 154 tick errors and entry
debits ranging $3.84–$58.32 on the same underlying, i.e. mispriced entries from garbage mids.

## Decisions (from brainstorm)

- Tastytrade is **primary** for everything: live stock quotes, option chains, option mids,
  VIX, and **historical candles** (backtests + charts).
- IBKR data code is **frozen as fallback**, not deleted; a `data_primary` setting
  (`"tastytrade"` | `"ibkr"`) allows instant revert.
- Gateway down → **monitor-only mode**: data/valuation/P&L stay live, order actions are
  skipped and journaled. Never blind, never trading without a broker.
- New **mid sanity guard** in the shared facade so a bad quote (either source) yields None
  (skip) rather than a mispriced trade.
- Non-goals: no Tastytrade order routing (session layer must not preclude a later phase),
  no greeks streaming (Builder keeps Black-Scholes), no IBKR data deletion until Tastytrade
  has soaked.

## Architecture

```
strategies / dashboard / builder / providers
        │
        ▼
app/marketdata/facade.py      ← single entry point for market data
   │ primary                      │ fallback (only when Gateway READY)
   ▼                              ▼
app/tastytrade/{session,streamer}.py     app/ibkr/* (existing, frozen)
```

## Components

### `app/tastytrade/session.py` — session manager
- **Credentials are OAuth, not username/password** (SDK v12 dropped password sessions):
  `tt_provider_secret` / `tt_refresh_token` in `Settings` (env: `TT_PROVIDER_SECRET`,
  `TT_REFRESH_TOKEN`), obtained from a Tastytrade OAuth personal grant
  (my.tastytrade.com → API access). Editable/persisted via the API tab.
- Uses the `tastytrade` SDK v12.4.1 (already installed in the venv; must be pinned in
  `backend/requirements.txt`). `Session(provider_secret=…, refresh_token=…)` is cheap to
  construct; the SDK fetches and auto-refreshes the short-lived access token internally
  (`session_expiration` tracked per request), so there is no daily manual login at all.
- Lazily creates and caches one `Session`. Missing creds or a failed probe marks Tastytrade
  unavailable (facade falls back) and journals once per transition.
- Exposes `async get_tt_session() -> Session | None` and `def reset_tt_session() -> None`
  (called after creds change).

### `app/tastytrade/streamer.py` — DXLink streamer
- One shared `DXLinkStreamer` websocket for the whole app, created on first use, auto-reconnect
  with exponential backoff; re-auth via the session manager when the token expired.
- **Quotes:** subscribe on demand to `Quote` events; keep an in-memory
  `{symbol: (bid, ask, last, received_at)}` cache. `await quote(symbol, timeout)` subscribes
  if new and waits for the first event, else returns the cached value. DXFeed serves
  last-known quotes after hours — this directly fixes the null-mid problem.
- **Symbols:** stocks plain (`QQQ`), index VIX as its dxfeed symbol, options converted from
  our `(underlying, expiry: date, strike, right)` tuple to the **streamer symbol provided by
  the Tastytrade instruments API** (each chain item carries its `streamer-symbol`; we map from
  our tuple to that rather than hand-building dxfeed option symbology).
- **Candles:** `bars(symbol, interval, start, end)` via DXLink `Candle` events with a
  from-time (e.g. `QQQ{=5m}`), collected until the window is covered, returned as the same
  Bar shape `ibkr_historical` produces.

### `app/marketdata/facade.py` — provider-agnostic operations
The five operations the app actually uses:

| Operation | Tastytrade path | IBKR fallback path |
|---|---|---|
| `stock_quote(symbol)` | streamer Quote | `ibkr/market.get_quotes` |
| `option_chain(underlying)` | REST nested option chains (expiries → strikes + streamer-symbols) | existing chain fetch |
| `option_mid(underlying, expiry, strike, right)` | streamer Quote on the option's streamer-symbol → (bid+ask)/2 | the current IBKR option-mid fetch, extracted from `LivePriceSource` into an ibkr-side helper (LivePriceSource itself delegates to the facade, so it cannot be the fallback) |
| `vix()` | streamer Quote on VIX | the current IBKR VIX fetch (`Index("VIX","CBOE")`), extracted the same way |
| `bars(symbol, interval, start, end)` | streamer Candle history | `providers/ibkr_historical` |

- Order per call: if `data_primary == "tastytrade"`: try TT → on error/timeout/None, if
  Gateway READY try IBKR → else None. (`data_primary == "ibkr"` reverses the order.)
- Journal an event only when the **serving source changes** (e.g. "market data: fell back to
  IBKR", "market data: Tastytrade restored"), not per call.
- Strategies already fail closed on None (no entry without VIX, skip tick without spot);
  that behavior is unchanged.

### Mid sanity guard (in the facade, applies to both sources)
`option_mid` returns **None** (never a suspect number) when:
- bid/ask crossed (`ask < bid`) or non-positive;
- spread too wide: `(ask − bid) > max(sanity_abs_spread, sanity_rel_spread × mid)`;
- jump too large: mid deviates more than `sanity_max_jump_pct` from the last **accepted** mid
  for the same contract observed within `sanity_jump_window_seconds`.
Rejections are journaled (warning, with the rejected values). Config defaults:
`sanity_rel_spread = 0.35` (35% of mid), `sanity_abs_spread = 2.50` ($), 
`sanity_max_jump_pct = 0.50` (50%), `sanity_jump_window_seconds = 900`.
This makes a repeat of the $58-debit-on-a-$4-spread entry impossible.

### Rewired consumers
- `strategies/triple_calendar/live_source.py` — `aunderlying`/`achain`/`aoption_mid`/`avix`
  delegate to the facade (signatures unchanged; the loop and core don't change).
- Dashboard / equity strategies — callers of `ibkr/market.get_quotes` go through
  `facade.stock_quote`.
- Builder — `routers/builder.py` chain / chain-quotes / quote endpoints use the facade.
- History — new `providers/tastytrade_historical.py` implementing `HistoricalDataProvider`
  via `facade.bars`; provider selection prefers it, `ibkr_historical` stays as fallback.
- **Order execution, reconciliation, kill switch: untouched** (IBKR, `ENABLE_LIVE_ORDERS`
  gate unchanged).

### Monitor-only mode (Gateway down)
Today the TC loop pauses entirely unless Gateway READY + market open. New behavior, while the
market is open:
- **Data side always runs:** spot/chain/mids/VIX via the facade, position valuation and
  `update_value` persist, dashboard and P&L stay live.
- **Order actions require Gateway READY:** when not READY, the tick is *valuation-only* —
  `manage_step` is not called (so no enter/roll/adjust/exit decisions fire), and the loop
  journals the monitor-only transition once. When READY returns, full ticks resume.
- Equity strategy loops keep their current structure — only their quote reads move to the
  facade; their order paths already gate on the Gateway connection, which is unchanged.

### Frontend
- API tab: Tastytrade card with OAuth provider-secret / refresh-token fields (persisted
  server-side to `.env`), configured/available status.
- Dashboard status: show two chips — **Data** (Tastytrade OK / fallback IBKR / down) and
  **Broker** (Gateway connected / disconnected) — replacing the single broker-implies-data
  status.

### Config additions
`tt_provider_secret`, `tt_refresh_token`, `data_primary` (default `"tastytrade"`), the four
sanity-guard settings above.

### Explicit scope notes (from code exploration)
- `services/market_stream.py` (the dashboard's persistent IBKR ticker stream for the
  WebSocket push) **stays on IBKR**; its REST fallback (`get_market_overview`) moves to the
  facade, so the overview degrades to Tastytrade-served REST when the Gateway is down.
- `options/chain.py` (Builder) gets mids through `LivePriceSource`, which delegates to the
  facade — only its spot quote and expiry-list fetches need explicit facade calls.
- Consumers that early-return on `session_manager.is_authenticated()` (overview, position
  live quote) drop that gate — data availability is the facade's concern now.

## Error handling

- TT login/websocket failure → facade falls back per call; journal on transition only.
- Both sources down → operations return None; strategies skip (fail-closed); existing
  alerting (`services/alerts.py`) covers the disconnect notifications.
- Sanity-guard rejection → None + journal warning.
- Session/streamer never raise into strategy loops; all failures surface as None returns.

## Testing

- **Unit (fakes, no network):** session re-auth on expiry; tuple→streamer-symbol mapping;
  each sanity rule (crossed, wide-spread, jump) rejects and passes correctly; facade order
  (TT ok → TT; TT fails + READY → IBKR; TT fails + not READY → None; `data_primary="ibkr"`
  reversed).
- **Loop-level (fakes):** Gateway-not-READY tick updates position value but never calls the
  executor (monitor-only); READY tick unchanged from today (regression: existing standalone
  TC live tests must keep passing).
- **Live smoke (the payoff check):** after hours, `facade.option_mid` for QQQ ~21-DTE
  strikes and `facade.vix()` return non-null via Tastytrade — the exact case IBKR failed
  (100% missing) on 2026-07-09.

## Non-goals

- No Tastytrade order routing (later phase; session layer is reusable for it).
- No greeks/streaming for the Builder (Black-Scholes stays).
- No removal of IBKR data code until Tastytrade has soaked in daily use.
