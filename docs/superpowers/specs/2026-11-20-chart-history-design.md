# Chart history: local 1-minute store, scroll-back, and go-to-date

Date: 2026-11-20 · Status: draft for approval

## Intent

The Chart screen should show deep price history instantly and keep real-time data for the
moving edge only. Today every candle request goes through the Tastytrade DXLink candle
collector, which serves one symbol at a time (4–12 s each) and only keeps about 3–4 weeks
of 1-minute data. Charts therefore load slowly, fall back to sample candles under load,
and cannot look back further than a few days on intraday timeframes.

Purpose (from the user): **displaying history on the chart**. Backtesting and strategy
use are not goals of this work.

## Decided by the user

- Tickers: **SPY, QQQ, TSLA, NVDA, SPX, NDX** only.
- **SPY, QQQ, TSLA, NVDA**: 1-minute history from **January 2022**, downloaded
  automatically in the background and kept up to date.
- **SPX, NDX**: only the **last 2 weeks** of 1-minute data (Tastytrade's limit). No deeper
  history and no long-term archive for now.
- **Real-time data** (Tastytrade live feed) is used **only for tickers open on the chart,
  during regular market hours**, to move the newest candle. Raw ticks are never saved.
- The chart **loads older candles as you scroll left**, back to the earliest stored bar.
- A **go-to-date box** jumps the chart to a chosen date.
- Build approach **A** (below).

## Goals

1. Opening a chart pane for any of the six tickers shows candles in well under a second,
   with no dependency on the DXLink candle queue for history.
2. Scrolling left keeps loading older candles until the start of available data, on every
   timeframe.
3. Go-to-date lands the chart on the chosen date; scrolling both ways from there works.
4. The newest candle updates in real time during regular hours for tickers on the chart.
5. History survives service restarts and tops itself up with no manual step.

## Non-goals

- Backtests or strategies reading this store (they keep their current data paths).
- Tickers beyond the six; a UI to add tickers.
- SPX/NDX history older than what Tastytrade holds (~2–4 weeks for 1m). Their daily
  history back to 2022 *is* available from Tastytrade and could be added later.
- Storing live ticks, quotes, or live-built candles.
- Changing the overview/dashboard quote streaming.

## Anti-goals (failure even if it "works")

- Chart history requests competing with live strategies for the DXLink candle collector.
- A split (TSLA 2022, NVDA 2024, any future one) showing as a fake crash or jump.
- Tests writing to the real history file, or two processes syncing it at once.
- The browser downloading years of minute bars at once (the SVG chart draws ≤400 bars).

## Constraints (from the code and probes)

- Alpaca market data with the existing `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` returns
  **SIP 1-minute bars back to 2022-01-03** (`GET https://data.alpaca.markets/v2/stocks/bars`,
  `feed=sip`), including bars from 20:55 UTC today. Use `adjustment=split` (raw NVDA
  2022-01-03 09:30 close 298.41 vs split-adjusted 29.84). Pages are ≤10,000 bars with
  `next_page_token`. Assume a ~200 requests/minute limit (the user's own downloader runs at
  180 rpm). Whether the account can read the latest 15 minutes of SIP is unknown — design
  for a ~15-minute lag on recent bars.
- Alpaca has **no cash indices** (SPX, NDX). Tastytrade DXLink 1m candles: SPX from
  2026-10-27, NDX from 2026-10-30 when probed on 2026-11-20 (~7,500 bars); daily from 2022.
- The live server runs code from a separate worktree (`/Users/karanpatel/Tradding-system-live`,
  detached at local `main`, refreshed on every restart by `scripts/run_live.sh`). Shared
  files are pointed at the dev checkout via env vars in that script (`DATABASE_URL`,
  `HISTORY_CACHE_DIR`, `SESSION_STORE_PATH`). Paths default to `backend/` via
  `config._BASE_DIR`.
- Tests refuse to run without a throwaway `DATABASE_URL` (`backend/tests/__init__.py`).
- Size: ~960 extended-hours minutes/day × ~1,230 trading days ≈ 1.2M rows per stock,
  ~4.7M rows total (a few hundred MB in SQLite). First download ≈ 470 pages ≈ a few minutes
  at 180 rpm.
- Chart frontend: custom SVG (`frontend/src/components/charts/PriceChart.jsx`), view =
  `{length ≤ 400, offset}` over a bars array; data via `marketData.js` → `api.marketBars`
  → `GET /api/market/bars`. Intervals today: 1m, 5m, 15m, 30m, 1h, 1D.

## Approaches considered

- **A. SQLite history file + background job in the backend (chosen).** One local DB of
  1-minute bars for the four stocks, filled from Alpaca by a task inside the service;
  coarser timeframes built from minutes; SPX/NDX served from Tastytrade with a short
  in-memory cache. Instant reads, survives restarts, no new infrastructure. Most to build.
- **B. Per-month files per ticker** (like the user's research downloader). Reusable by
  research scripts, but range reads across years open dozens of files and the code is
  clumsier.
- **C. Fetch on demand, no background job.** Least code, but the first look at any range
  waits on Alpaca, contradicting "download automatically in the background".

The user handed the build choice to Claude; A is the only option that makes every chart
open instant.

## Design

### 1. History store (backend)

- New SQLite file `backend/market_history.db` (git-ignored via `*.db`), path setting
  `market_history_path` (env `MARKET_HISTORY_PATH`), default `_BASE_DIR / "market_history.db"`.
- `scripts/run_live.sh` exports `MARKET_HISTORY_PATH="$REPO/backend/market_history.db"` so
  the live worktree shares the file with the dev checkout.
- *Claude's call:* **only the live service syncs.** A setting `market_history_sync`
  (env `MARKET_HISTORY_SYNC`, default `false`) gates the background job; `run_live.sh`
  exports `MARKET_HISTORY_SYNC=true`. A dev backend reads the same file but never writes
  it, so there is one writer and one Alpaca request budget. The job also takes an
  exclusive lock file next to the DB and stands down if another process holds it.
- Deploy note: the LaunchAgent runs an installed copy of `run_live.sh` in
  `~/Library/Application Support/TradingSystem/`, so `scripts/install_autostart.sh`
  (with `RUN_WEB_HOST=0.0.0.0`, LAN mode) must be re-run once for the new exports to take
  effect. Coordinate with the system-maintenance session.
- Tests must never open the real file: the test guard also requires a throwaway
  `MARKET_HISTORY_PATH` (or the store is disabled under tests).
- Tables:
  - `bars_1m(symbol, ts, open, high, low, close, volume)` — `ts` = bar start, epoch ms
    UTC; primary key `(symbol, ts)`; split-adjusted prices; 04:00–20:00 ET.
  - `bars_1d(symbol, session_date, open, high, low, close, volume)` — built from
    **regular-session** (09:30–16:00 ET) minutes; rebuilt for a day whenever its minutes
    change.
  - `sync_state(symbol, earliest_ts, latest_ts, last_full_check, status, note)`.

### 2. Background sync job (backend)

- Started from the FastAPI lifespan like the existing refreshers; never blocks startup.
- On start: for each of SPY, QQQ, TSLA, NVDA, download any missing range from 2022-01-03
  to now−16 min, paging with `next_page_token`, throttled to ≤180 requests/min, writing in
  transactions per page (crash-safe and resumable: it continues from `latest_ts`).
- Then: every 5 minutes on trading days between 04:00 and 20:15 ET, fetch from
  `latest_ts` to now−16 min. One final pass after 20:15 ET.
- Split safety: each top-up re-fetches the previous trading day; if stored closes differ
  from the freshly adjusted ones by more than rounding, re-download that ticker's whole
  history (a split changed past prices).
- Errors back off and retry; they never crash the service. Status is visible via
  `GET /api/market/history/status` (per symbol: earliest, latest, status, note).
- Trading days come from `app/services/market.py` (`NYSE_HOLIDAYS`, full closures only;
  extend it past 2027 as needed). Half-days need no special case: the job stores whatever
  minutes Alpaca returns.

### 3. SPX and NDX (backend)

- 1-minute bars for the last 14 days from the Tastytrade candle collector, held **in
  memory** (not written to disk), fetched only when a chart asks. *Claude's call:* cached
  for 10 minutes, and a cold first open may take a few seconds (one collector request per
  symbol per 10 minutes at most). No background preloading, so SPX/NDX charts add no
  steady load on the collector the XSP strategy uses.
- Intraday SPX/NDX timeframes (1m–4h) are built from those minutes and cover ~2 weeks.
- *Claude's call:* SPX/NDX **1D and 1W keep today's behaviour** (Tastytrade daily candles,
  ~400 days, via the existing `/api/market/bars` path and its 45 s cache) so the daily
  chart doesn't regress to ~10 bars.

### 4. History API (backend)

`GET /api/market/history?symbol=SPY&interval=5m&before=<ms>&limit=600`
`GET /api/market/history?symbol=SPY&interval=5m&after=<ms>&limit=600`
`GET /api/market/history?symbol=SPY&interval=5m&around=<YYYY-MM-DD>&limit=600`

- Intervals: `1m, 5m, 15m, 30m, 1h, 4h, 1D, 1W`. Intraday intervals are aggregated
  from `bars_1m` for the requested window, bucketed on ET clock boundaries within each
  session; `1D` reads `bars_1d`; `1W` aggregates `bars_1d` by ET week.
- Response: `{symbol, interval, bars: [...oldest first], earliest, latest, has_more_before,
  has_more_after, source, note}` with the same bar shape as `/api/market/bars`.
- `around` returns ~`limit/2` bars on each side of the date's first regular-session bar
  (or the nearest available bar if the date is a weekend, holiday, or outside history).
- No `before`/`after`/`around` → the newest `limit` bars.
- Open (no app token), like the other market reads; symbol must be one of the six.
- *Claude's call:* the newest minutes **never come from the DXLink candle collector.**
  The sync job asks Alpaca for SIP bars up to now; if the account can't read the latest
  15 minutes of SIP, it fetches that tail from Alpaca's real-time `feed=iex` instead and
  replaces it with SIP bars on the next top-up. During market hours the job runs every
  minute for the four stocks (4 requests/min), so stored history is ≤ ~1–2 minutes behind.
  The API only reads the store.

### 5. Real-time edge (backend + frontend)

- During regular hours only, the chart's newest candle updates from the live Tastytrade
  feed, for symbols currently open in a chart pane. No live data is persisted.
- *Claude's call:* **push, not 15 s polling** (today `useQuotes` polls only watchlist and
  alert symbols every 15 s, and `/ws` pushes only the overview's five ETFs). The browser
  tells `/ws` which symbols are open in chart panes (a `chart_symbols` message); the server
  subscribes those to the existing DXLink Quote/Trade stream and pushes `{symbol, price,
  ts}` at most once per second per symbol. The frontend folds each price into the last
  candle and opens a new candle at each interval boundary (volume shown from stored bars
  only). The overview stream is unchanged.
- Outside regular hours the chart shows stored data only (up to ~16 min behind in
  extended hours for the stocks).

### 6. Chart (frontend)

- `marketData.js`: replace the one-shot `marketBars` load with a paged history loader per
  `(symbol, interval)`:
  - initial load: newest ~600 bars;
  - when the view's left edge comes within ~100 bars of the loaded start and
    `has_more_before`, fetch the previous page and prepend it (the view offset counts from
    the newest bar, so prepending needs no offset change);
  - after go-to-date, symmetric paging to the right until `has_more_after` is false
    (appending on the right does need the offset shifted to keep the view still);
  - indicator warm-up: keep ≥ the longest study lookback of extra bars loaded behind the
    view.
- `PriceChart.jsx`: draw a faint "History starts" marker at `earliest` when it is on
  screen; keep the 400-bar view cap.
- Toolbar: a **go-to-date** control (date input). Choosing a date loads `around=<date>`
  and centres it; a **"Live"** button returns to the newest bars.
- Timeframe menu: add `4h` and `1W`.
- *Claude's call:* **other symbols keep today's behaviour.** The default layout includes
  IWM, AAPL and MSFT and symbol search accepts anything, so symbols outside the six keep
  the current one-shot `/api/market/bars` loader (real Tastytrade candles, short
  lookback, no scroll-back or go-to-date). `marketData.js` routes the six to the paged
  history loader and everything else to the existing one. 4h and 1W for other symbols are
  aggregated client-side from what the existing path returns. The sample-data fallback
  stays only for when a request fails.

## Left to the builder

- Exact page sizes and prefetch thresholds; SQLite pragmas (WAL, synchronous) and
  indexes.
- Whether intraday aggregation runs in SQL or Python (target: <100 ms per 600-bar page).
- How the frontend tracks "symbols open in a chart pane" for the live edge.
- Exact backoff timings and status wording.
- Work branches from `main` (`scripts/run_live.sh` exists only there).
- Split re-check tolerance.

## Testing

- Store and aggregation: pure functions with fixtures (ET bucket edges, half-days,
  DST changes, empty ranges).
- Sync job: fake Alpaca client (pages, `next_page_token`, rate limit, errors, resume after
  crash, split re-download).
- API: `before`/`after`/`around` boundaries, `earliest`/`has_more_*`, unknown symbol → 400,
  SPX/NDX path with a fake streamer.
- No test touches the network, the real `market_history.db`, or `trading.db`.
- Frontend: paging math (prepend keeps view still) as a `.test.mjs` like `chartMath`.
