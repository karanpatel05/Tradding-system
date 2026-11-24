# Chart History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deep, instant chart history for SPY/QQQ/TSLA/NVDA (1-minute from 2022 via Alpaca, stored locally) and SPX/NDX (last 2 weeks from Tastytrade, in memory), with scroll-back, go-to-date, 4h/1W, and a pushed real-time last candle.

**Architecture:** A new `backend/app/history/` package owns a SQLite minute store, pure aggregation, an Alpaca bars client, a background sync job (live service only), and a read service behind `GET /api/market/history`. The `/ws` channel gains a `chart_symbols` message and pushes last-trade prices for those symbols during regular hours. The chart frontend gets a paged history loader for the six symbols; other symbols keep today's loader.

**Tech Stack:** Python 3.14, FastAPI, stdlib `sqlite3` (via `asyncio.to_thread`), httpx, pytest + pytest-asyncio; React 18 + Vite, plain-node `.test.mjs` tests.

**Spec:** `docs/superpowers/specs/2026-11-20-chart-history-design.md` (read it first; it carries the reasons).

## Global Constraints

- Branch `feat/chart-history` in worktree `/Users/karanpatel/Tradding-system/.claude/worktrees/chart-history`. Never switch the shared checkout's branch.
- Python: `PY=/Users/karanpatel/Tradding-system/.venv/bin/python`. Backend tests, from `backend/`: `rm -f _t.db && DATABASE_URL=sqlite+aiosqlite:///./_t.db $PY -m pytest -q -p no:cacheprovider` (fresh DB every run; `*.db` is git-ignored).
- Frontend: if `frontend/node_modules` is missing, `ln -s /Users/karanpatel/Tradding-system/frontend/node_modules frontend/node_modules`. Tests: `node <file>.test.mjs`. Build: `npm run build` in `frontend/`.
- History symbols: `STOCK_SYMBOLS = ("SPY", "QQQ", "TSLA", "NVDA")`, `INDEX_SYMBOLS = ("SPX", "NDX")`.
- Stock history start: `2022-01-03` (first bar 09:30 ET = 14:30 UTC). Alpaca: `https://data.alpaca.markets/v2/stocks/bars`, `timeframe=1Min`, `feed=sip`, `adjustment=split`, `limit=10000`, `next_page_token` paging, ≤ 180 requests/minute, headers from `app.broker.alpaca_client.headers()`.
- Minute bars stored for 04:00–20:00 ET; daily bars from regular session 09:30–16:00 ET only.
- Intervals served: `1m, 5m, 15m, 30m, 1h, 4h, 1D, 1W`. Intraday buckets on ET clock boundaries (1h on the hour, 4h at 00/04/08/12/16/20 ET); 1W = ET week starting Monday.
- Bar shape everywhere: `{"time": <epoch ms UTC bar start>, "open", "high", "low", "close", "volume"}`.
- SPX/NDX intraday: 14 days of 1m from the Tastytrade candle collector, in memory, TTL 600 s. SPX/NDX 1D/1W: existing `app.services.chart_data.get_bars(symbol, "1d")`.
- The history API and the sync job **never** call the DXLink candle collector for the four stocks.
- Only the process with `MARKET_HISTORY_SYNC=true` writes the history file; it also holds `<db path>.lock` (`fcntl.flock`, non-blocking).
- No test touches the network, the real `backend/market_history.db`, or `trading.db`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.

## Review Focus

1. **Sync interrupted mid-download (service restart during the first backfill)** — the next run resumes from the last stored minute with no gap and no duplicate rows. Test: Task 4 `test_backfill_resumes_after_crash`.
2. **DST change days (2026-03-08, 2026-11-01) and half-days (2026-11-27 closes 13:00)** — 1h/4h/1D buckets stay on ET clock times and a half-day's daily bar ends at its last minute. Tests: Task 2 `test_buckets_follow_et_across_dst`, `test_half_day_daily_bar`.
3. **Go-to-date on a weekend, holiday, before 2022-01-03 or in the future** — the chart lands on the nearest available bar, never an error or an empty pane. Test: Task 5 `test_around_snaps_to_nearest_available`.
4. **Alpaca keys missing, wrong (401) or rate-limited (429)** — sync backs off, status shows a readable note, the service keeps running and the API serves what is stored. Test: Task 4 `test_auth_and_rate_errors_are_contained`.
5. **Fast scrolling triggers the "near edge" check many times** — one page request in flight per direction, and pages merge without duplicates or reordering. Test: Task 7 `pagingDedupes`.

---

### Task 1: History store and settings

**Files:**
- Create: `backend/app/history/__init__.py` (empty), `backend/app/history/store.py`
- Modify: `backend/app/config.py` (two settings next to `history_cache_dir`), `backend/tests/__init__.py` (history-path guard)
- Test: `backend/tests/test_history_store.py`

**Interfaces:**
- Produces:
  - Settings `market_history_path: str = str(_BASE_DIR / "market_history.db")` (env `MARKET_HISTORY_PATH`), `market_history_sync: bool = False` (env `MARKET_HISTORY_SYNC`).
  - `class HistoryStore(path: str)` — synchronous; opens with WAL; creates tables on first open.
    - `upsert_minutes(symbol: str, bars: list[dict]) -> int` (rows written; replace on `(symbol, ts)`).
    - `minutes(symbol: str, start_ms: int, end_ms: int) -> list[dict]` (inclusive start, exclusive end, oldest first).
    - `upsert_daily(symbol: str, rows: list[dict]) -> None` (row = bar shape + `"session"`: `"YYYY-MM-DD"`, `time` = 09:30 ET of that date).
    - `daily(symbol: str, start_ms: int, end_ms: int) -> list[dict]`.
    - `bounds(symbol: str) -> tuple[int | None, int | None]` (earliest, latest minute ts).
    - `delete_symbol(symbol: str) -> None` (minutes + daily + state).
    - `get_state(symbol) -> dict | None`, `set_state(symbol, **fields) -> None` (fields: `status`, `note`, `last_full_check`).
  - `get_store() -> HistoryStore` (module singleton on `get_settings().market_history_path`).
- Tables: `bars_1m(symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY(symbol, ts)) WITHOUT ROWID`; `bars_1d(symbol TEXT, session TEXT, ts INTEGER, open, high, low, close, volume, PRIMARY KEY(symbol, session)) WITHOUT ROWID`; `sync_state(symbol TEXT PRIMARY KEY, status TEXT, note TEXT, last_full_check TEXT)`.

- [ ] **Step 1: Write the failing tests** (`tmp_path` for the DB):
  - `test_upsert_is_idempotent_and_ordered`: upsert 3 bars out of order, upsert one again with a new close → `minutes()` returns 3 rows sorted by `time`, the re-upserted close wins.
  - `test_minutes_range_is_half_open`: bars at 60_000, 120_000, 180_000; `minutes(s, 60_000, 180_000)` → times `[60_000, 120_000]`.
  - `test_bounds_and_delete`: `bounds` returns (min, max); after `delete_symbol` → `(None, None)` and `get_state` is None.
  - `test_state_round_trip`: `set_state("SPY", status="ok", note="")` then `set_state("SPY", note="x")` → `{"status": "ok", "note": "x", ...}`.
  - `test_tests_never_use_real_history_file`: `get_settings().market_history_path` does not end with `backend/market_history.db`.

- [ ] **Step 2: Run** `... -m pytest tests/test_history_store.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement** the settings, `store.py`, and the guard: in `tests/__init__.py`, if `MARKET_HISTORY_PATH` is unset set it to a file in `tempfile.gettempdir()` named `_t_market_history_<pid>.db`; if it resolves to `.../backend/market_history.db`, raise `RuntimeError` like the existing DB guard.

- [ ] **Step 4: Run** the new tests, then the full backend suite → all PASS.

- [ ] **Step 5: Commit** `feat(history): local SQLite minute store and settings`.

---

### Task 2: Aggregation (pure)

**Files:**
- Create: `backend/app/history/aggregate.py`
- Test: `backend/tests/test_history_aggregate.py`

**Interfaces:**
- Produces:
  - `INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}`.
  - `bucket(minutes: list[dict], interval: str) -> list[dict]` — groups 1m bars (oldest first) into buckets keyed by ET-local `floor(minute_of_day / n) * n` within each ET date; bucket `time` = UTC ms of the bucket's ET start; OHLC = first open / max high / min low / last close; volume summed. `"1m"` returns the input.
  - `daily_from_minutes(minutes: list[dict]) -> list[dict]` — one row per ET date from bars with ET time in [09:30, 16:00); row has `session` and `time` = 09:30 ET.
  - `weekly_from_daily(days: list[dict]) -> list[dict]` — groups by ET ISO week (Monday start); `time` = first day's `time`.
  - `et_session_start_ms(day: date) -> int` (09:30 ET as UTC ms).

- [ ] **Step 1: Write the failing tests** with helper `m(et_str, close)` building a 1m bar from `"2026-11-20 09:30"` in `America/New_York`:
  - `test_5m_buckets_on_clock`: 09:30..09:39 → two bars at 09:30 and 09:35, OHLC/volume correct.
  - `test_1h_and_4h_on_et_clock`: bars 07:58–08:02 and 09:30–10:01 → 1h buckets at 07:00, 08:00, 09:00, 10:00; 4h buckets at 04:00 and 08:00.
  - `test_buckets_follow_et_across_dst`: 2026-03-06 10:00 ET and 2026-03-09 10:00 ET (UTC hour differs by 1) → each 1h bucket `time` maps back to 10:00 ET.
  - `test_daily_excludes_extended_hours`: bars at 08:00, 09:30, 15:59, 16:00 ET → daily OHLC from 09:30 and 15:59 only.
  - `test_half_day_daily_bar`: 2026-11-27 bars 09:30–12:59 → one daily bar whose close is the 12:59 close.
  - `test_weekly_starts_monday`: daily rows Fri 2026-11-20, Mon 11-23, Tue 11-24 → two weekly bars, second `time` = Monday's.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with `zoneinfo.ZoneInfo("America/New_York")`. **Step 4: Run** → PASS.

- [ ] **Step 5: Commit** `feat(history): ET-aligned minute aggregation`.

---

### Task 3: Alpaca bars client

**Files:**
- Create: `backend/app/history/alpaca_bars.py`
- Test: `backend/tests/test_history_alpaca_bars.py`

**Interfaces:**
- Produces:
  - `class AlpacaDataError(Exception)` with `.status: int` and `.recent_sip_denied: bool` (True for a 403 whose message mentions recent SIP).
  - `class RateLimiter(per_minute: int = 180)` — `async acquire()`; spacing ≥ 60/per_minute s between calls.
  - `async fetch_minutes(symbol: str, start: datetime, end: datetime, *, feed: str = "sip", client: httpx.AsyncClient | None = None, limiter: RateLimiter | None = None) -> list[dict]` — follows `next_page_token` until exhausted; maps `t` (RFC 3339) → `time` ms, `o,h,l,c,v` → bar shape; sends the Global Constraints params; raises `AlpacaDataError` on non-200. Tests inject `client` built on `httpx.MockTransport`.

- [ ] **Step 1: Write the failing tests:**
  - `test_pages_are_followed_and_mapped`: transport returns page 1 with `next_page_token="p2"`, page 2 without → 3 bars, `time` of `"2022-01-03T14:30:00Z"` = 1641220200000; second request carries `page_token=p2`; every request has `feed=sip`, `adjustment=split`, `timeframe=1Min`, `limit=10000`.
  - `test_errors_raise_with_status`: 401 → `AlpacaDataError(status=401)`; 403 with `{"message":"subscription does not permit querying recent SIP data"}` → `recent_sip_denied is True`.
  - `test_limiter_spaces_calls`: `RateLimiter(per_minute=600)` (0.1 s); three `acquire()` take ≥ 0.2 s.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.

- [ ] **Step 5: Commit** `feat(history): Alpaca 1-minute bars client with paging and rate limit`.

---

### Task 4: Background sync job (live service only)

**Files:**
- Create: `backend/app/history/sync.py`
- Modify: `backend/app/main.py` (lifespan: start the job when `market_history_sync`), `scripts/run_live.sh` (exports), `README.md` (short "Chart history" section: what is stored, where, the two env vars, re-run `RUN_WEB_HOST=0.0.0.0 ./scripts/install_autostart.sh` once after deploy)
- Test: `backend/tests/test_history_sync.py`

**Interfaces:**
- Consumes: Task 1 `HistoryStore`, Task 2 `daily_from_minutes`, Task 3 `fetch_minutes`/`AlpacaDataError`, `app.services.market._is_trading_day`, `get_market_status`.
- Produces:
  - `HISTORY_START = datetime(2022, 1, 3, 14, 30, tzinfo=UTC)`.
  - `class HistorySync(store: HistoryStore, fetch=fetch_minutes, now=lambda: datetime.now(UTC))`
    - `async sync_symbol(symbol: str) -> int` — fetch `[latest+1min or HISTORY_START, now)` with `feed="sip"`; on `recent_sip_denied`, fetch up to `now − 16 min` with SIP and the tail with `feed="iex"`; upsert per page batch; rebuild daily rows for every ET date touched; `set_state(status="ok", note="")`. Returns rows written.
    - `async check_splits(symbol: str) -> bool` — re-fetch the previous trading day; if any stored close differs from the fetched one by > 0.0001 relative, `delete_symbol` and return True (caller then re-syncs from `HISTORY_START`).
    - `async run_once() -> None` — for each `STOCK_SYMBOLS`: `check_splits` at most once per ET day (tracked in `last_full_check`), then `sync_symbol`. Errors per symbol → `set_state(status="error", note=<short reason>)`, never raised.
    - `next_delay(now: datetime) -> float` — 60 s during regular hours, 300 s in 04:00–20:15 ET on trading days, otherwise seconds until 04:00 ET of the next trading day (one pass at ≥ 20:15 is guaranteed by the 300 s cadence).
  - `async run_forever(sync: HistorySync) -> None` — takes `<path>.lock` with `fcntl.flock(LOCK_EX | LOCK_NB)`; if held, logs and returns; loop `run_once` / sleep `next_delay`; on repeated errors, back off up to 15 min.
  - `def history_status() -> dict` — `{symbol: {"earliest": ms|None, "latest": ms|None, "status": str, "note": str}}` for the four stocks.
  - `scripts/run_live.sh`: `export MARKET_HISTORY_PATH="$REPO/backend/market_history.db"` and `export MARKET_HISTORY_SYNC=true` next to the existing exports.

- [ ] **Step 1: Write the failing tests** with a fake `fetch` that serves bars from a dict and can raise on demand, and a fixed `now`:
  - `test_first_sync_starts_at_2022_and_builds_daily`: empty store → first fetch `start == HISTORY_START`; daily rows exist for each ET date served.
  - `test_backfill_resumes_after_crash`: fake raises after the first page batch; second `sync_symbol` starts at last stored ts + 60 000; final rows = union, no duplicates.
  - `test_recent_sip_denied_uses_iex_tail`: first SIP call raises `recent_sip_denied` → one SIP call ending at `now − 16 min` and one `feed="iex"` call for the tail.
  - `test_split_triggers_full_redownload`: stored previous-day close 298.41, fetched 29.84 → `check_splits` returns True and the symbol's rows are gone.
  - `test_auth_and_rate_errors_are_contained`: fake raises `AlpacaDataError(401)` for SPY and `AlpacaDataError(429)` for QQQ → `run_once` completes, TSLA/NVDA synced, SPY/QQQ state `status="error"` with notes naming the status.
  - `test_next_delay`: regular hours → 60; 18:00 ET trading day → 300; Saturday noon → until Monday 04:00 ET.
  - `test_second_process_stands_down`: hold the lock in the test, `run_forever` returns without calling `fetch`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (store calls via `asyncio.to_thread`). Wire lifespan: `if settings.market_history_sync: asyncio.create_task(run_forever(HistorySync(get_store())))`.

- [ ] **Step 4: Run** the new tests and the full suite → PASS.

- [ ] **Step 5: Commit** `feat(history): background Alpaca sync for the live service`.

---

### Task 5: History read service and API

**Files:**
- Create: `backend/app/history/service.py`
- Modify: `backend/app/routers/dashboard.py` (two GET routes, no `require_auth`)
- Test: `backend/tests/test_history_api.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4 (`history_status`); `app.marketdata.facade.bars`; `app.services.chart_data.get_bars`, `normalize_symbol`.
- Produces:
  - `HISTORY_INTERVALS = ("1m","5m","15m","30m","1h","4h","1D","1W")`; `DEFAULT_LIMIT = 600`, `MAX_LIMIT = 2000`.
  - `class HistoryRequestError(ValueError)` → HTTP 400.
  - `async get_history(symbol: str, interval: str, *, before: int | None = None, after: int | None = None, around: date | None = None, limit: int = DEFAULT_LIMIT) -> dict` returning `{symbol, interval, bars, earliest, latest, has_more_before, has_more_after, source, note}`.
    - Stocks: read the store over a window wide enough to yield `limit` buckets (intraday: fetch minutes in chunks backward/forward until `limit` buckets or the bounds are hit; `1D`/`1W` from `daily`).
    - Indexes intraday: `_index_minutes(symbol)` = `facade.bars(symbol, "1m", now − 14 days)` cached 600 s in a module dict, then `bucket`; `source="tastytrade"`.
    - Indexes `1D`/`1W`: `chart_data.get_bars(symbol, "1d")` bars (`1W` via `weekly_from_daily`).
    - `around`: target = `et_session_start_ms(around)`; return `limit // 2` bars with `time < target` and the rest from target on; if the date has no bars, use the nearest available side.
    - None of `before/after/around` → newest `limit` bars. More than one given → `HistoryRequestError`.
  - Routes: `GET /api/market/history` (query `symbol, interval="5m", before, after, around (YYYY-MM-DD), limit`) and `GET /api/market/history/status` → `history_status()`.

- [ ] **Step 1: Write the failing tests** (store fixture filled with 3 trading days of SPY minutes; `facade.bars` and `chart_data.get_bars` monkeypatched):
  - `test_newest_page_and_flags`: `limit=50, interval="5m"` → 50 bars, ascending, last = newest bucket, `has_more_before is True`, `has_more_after is False`, `earliest`/`latest` match store bounds.
  - `test_before_pages_back_without_overlap`: page 1 then `before=page1[0]["time"]` → page 2 ends strictly before page 1 starts.
  - `test_around_snaps_to_nearest_available`: `around` = a Saturday, a NYSE holiday, 2020-01-02, and 2030-01-02 → each returns bars and no error; 2020 starts at `earliest`; 2030 ends at `latest`.
  - `test_index_intraday_uses_14_day_cache`: two calls for SPX 5m → `facade.bars` called once with interval `"1m"`; `earliest` = first cached minute.
  - `test_index_daily_uses_existing_daily_path`: SPX `1D` → bars from the patched `chart_data.get_bars(…, "1d")`.
  - `test_stocks_never_touch_collector`: SPY any interval → patched `facade.bars` not called.
  - `test_bad_requests_are_400` (TestClient on the router): unknown symbol `AAPL`, interval `2h`, `limit=0`, both `before` and `after` → 400; lowercase `spy` → 200.
  - `test_history_routes_are_open`: neither route has `require_auth` (same check as `tests/test_market_reads_open.py`).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** new + full suite → PASS.

- [ ] **Step 5: Commit** `feat(history): paged history API for the chart`.

---

### Task 6: Live last-candle push on `/ws`

**Files:**
- Create: `backend/app/services/chart_live.py`
- Modify: `backend/app/routers/ws.py` (receive loop + snapshot field)
- Test: `backend/tests/test_chart_live.py`

**Interfaces:**
- Consumes: `app.tastytrade.streamer.get_streamer()` (`_subscribe(Trade, symbol)`, `cached_trade(symbol) -> (price, monotonic_ts)`), `get_market_status()`.
- Produces:
  - `MAX_CHART_SYMBOLS = 12`.
  - `class ChartLive` — `async set_symbols(conn_id: str, symbols: list[str]) -> None` (normalizes via `normalize_symbol`, drops invalid, caps at 12, subscribes new ones to Trade); `forget(conn_id) -> None`; `prices(conn_id: str) -> dict[str, dict]` → `{symbol: {"price": float, "ts": epoch_ms}}` for that connection's symbols with a cached trade, **empty unless `get_market_status()["session"] == "regular"`**.
  - `chart_live = ChartLive()` singleton.
  - `/ws`: a concurrent receive task reads JSON frames; `{"type": "chart_symbols", "symbols": [...]}` → `chart_live.set_symbols(id(websocket) as str, symbols)`. Each snapshot adds `"chart_prices": chart_live.prices(conn_id)`. On disconnect → `forget`.

- [ ] **Step 1: Write the failing tests** with a fake streamer (records subscriptions, returns set prices) and patched market status:
  - `test_prices_only_in_regular_session`: session `"after_hours"` → `{}`; `"regular"` → `{"SPY": {"price": 690.5, "ts": ...}}`.
  - `test_symbols_are_normalized_capped_and_per_connection`: `["spy", "bad sym", *13 valid]` → 12 symbols, uppercase, invalid dropped; conn B sees none of conn A's.
  - `test_forget_clears_connection`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** and wire `/ws` (keep the existing send loop unchanged apart from the new field). **Step 4: Run** new + full suite → PASS.

- [ ] **Step 5: Commit** `feat(chart): push live last-trade prices for chart symbols on /ws`.

---

### Task 7: Frontend history loader and timeframes

**Files:**
- Create: `frontend/src/components/charts/historyPaging.js`, `frontend/src/components/charts/historyPaging.test.mjs`
- Modify: `frontend/src/api.js` (`marketHistory`), `frontend/src/components/charts/marketData.js` (`useHistoryBars`, routing), `frontend/src/components/charts/controls/Menus.jsx` (`INTERVAL_LABELS` + menu rows for `4h`, `1W`), `frontend/src/components/charts/ChartWorkspace.jsx` (panes use the router hook)

**Interfaces:**
- Produces:
  - `api.marketHistory({symbol, interval, before, after, around, limit})` → `GET /api/market/history`.
  - `HISTORY_SYMBOLS = ["SPY","QQQ","TSLA","NVDA","SPX","NDX"]` (export from `historyPaging.js`).
  - Pure helpers in `historyPaging.js`:
    - `mergePage(bars, page, side) -> bars` — `side` `"before"|"after"`; dedupe by `time`, keep ascending.
    - `shouldLoad(view, total, loaded, side, threshold = 100) -> boolean` — `view = {length, offset}` as in `PriceChart` (offset counted from the newest bar); `loaded = {hasMoreBefore, hasMoreAfter, inFlight: {before, after}}`.
    - `offsetAfterAppend(offset, appendedCount) -> number` (offset + appendedCount; prepend needs no change).
  - `useHistoryBars(symbol, interval) -> {bars, live, stale, note, earliest, hasMoreBefore, hasMoreAfter, loadMore(side), goToDate(isoDate), backToLive()}`. Initial page `limit=600`; at most one request in flight per side; on `symbol/interval` change, discard late responses.
  - `usePaneBars(symbol, interval, background)` — returns `useHistoryBars` for `HISTORY_SYMBOLS`, else today's `useBars` (with `4h` = `["15m", 16]` and `1W` = `["1d", 5]` added to `FEED_INTERVAL`).

- [ ] **Step 1: Write the failing test file** `historyPaging.test.mjs` (same `eq` style as `chartMath.test.mjs`):
  - `mergeKeepsOrder`: merge before/after pages with one overlapping `time` → ascending, unique.
  - `pagingDedupes`: `shouldLoad` returns false while `inFlight.before` is true and when `hasMoreBefore` is false; true when the view's left edge is within 100 bars of index 0.
  - `appendShiftsOffset`: `offsetAfterAppend(20, 600) === 620`.

- [ ] **Step 2: Run** `node frontend/src/components/charts/historyPaging.test.mjs` → FAIL. **Step 3: Implement** helpers, hooks, API method, menu entries. **Step 4: Run** the test file and `node frontend/src/components/charts/chartMath.test.mjs` → PASS; `npm run build` succeeds.

- [ ] **Step 5: Commit** `feat(chart): paged history loader for the six symbols; 4h and 1W`.

---

### Task 8: Scroll-back, history marker, go-to-date, live candle

**Files:**
- Modify: `frontend/src/components/charts/PriceChart.jsx`, `frontend/src/components/charts/ChartWorkspace.jsx`, `frontend/src/hooks/useWebSocket.js`, `frontend/src/components/charts/charts.css`
- Create: `frontend/src/components/charts/liveCandle.js`, `frontend/src/components/charts/liveCandle.test.mjs`

**Interfaces:**
- Consumes: Task 7 `usePaneBars`/`useHistoryBars`, `shouldLoad`, `offsetAfterAppend`; Task 6 `chart_prices` snapshot field and `chart_symbols` message.
- Produces:
  - `PriceChart` new props: `onNearEdge(side)` (called when `shouldLoad` says so after any view change, with `threshold = max(100, longest active study lookback)` so indicators always have warm-up bars loaded behind the view), `earliest` (ms; draws a dashed vertical line labelled "History starts" at that bar when visible), `focusTime` (ms; when it changes, set the view so that bar is centred), `appended` (count of bars just added on the right; shift offset with `offsetAfterAppend`).
  - Toolbar in `ChartWorkspace`: a date input labelled "Go to date" (min `2022-01-03`, max today) calling the active pane's `goToDate`; a "Live" button (shown when the active pane is not at the newest bar) calling `backToLive`.
  - `useSnapshot(enabled, chartSymbols = [])` sends `{"type":"chart_symbols","symbols":chartSymbols}` on open and whenever the list changes.
  - `foldLivePrice(bars, price, tsMs, intervalMs) -> bars` in `liveCandle.js`: if `tsMs` is inside the last bar's interval, update its close/high/low; if past it, append a new bar `{time: floor(tsMs/intervalMs)*intervalMs, open: price, high: price, low: price, close: price, volume: 0}`; if older, return `bars` unchanged. For `1D`/`1W` panes, update the last bar only (never append).

- [ ] **Step 1: Write the failing test** `liveCandle.test.mjs`: `updatesLastBar`, `opensNewBarAtBoundary`, `ignoresOldTicks`, `dailyNeverAppends`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `liveCandle.js`, the `PriceChart` props, toolbar controls, and `useSnapshot` change; panes fold `snapshot.chart_prices[symbol]` into their bars only when the pane is at the newest bar.
- [ ] **Step 4: Run** both `.test.mjs` files → PASS; `npm run build` succeeds.
- [ ] **Step 5: Commit** `feat(chart): scroll-back, history marker, go-to-date and live last candle`.

---

### Task 9: End-to-end verification (no deploy)

- [ ] **Step 1:** Full backend suite and both frontend test files → PASS; `npm run build` → success.
- [ ] **Step 2:** Run a dev backend from the worktree on port 8790 with `MARKET_HISTORY_PATH` set to a scratch file and `MARKET_HISTORY_SYNC=true` (a dev process may sync **only** into a scratch file, never `backend/market_history.db`). Confirm `/api/market/history/status` shows SPY progressing and, after the first pages land, `/api/market/history?symbol=SPY&interval=1D&around=2022-01-03` returns a first bar at 2022-01-03.
- [ ] **Step 3:** Open the chart against that backend in the browser pane: scroll back on SPY 5m loads older pages; go-to-date `2023-03-15` lands there; SPX 5m shows ~2 weeks with the "History starts" line; AAPL still shows real candles.
- [ ] **Step 4:** Stop the dev backend and delete the scratch file. Report results. Merging to `main`, re-running `install_autostart.sh` and restarting the live service are left to the user and the system-maintenance session.
