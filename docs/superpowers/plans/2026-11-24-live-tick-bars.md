# Live Trade Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** During regular trading hours, record every valid trade for a fixed symbol list into `market_history.db`, roll trades into 1-minute bars served to the Chart live, let official Alpaca bars supersede them, and delete trade-level data at the end of each day.

**Architecture:** A `LiveRecorder` task runs inside the existing history sync process, under the same `MARKET_HISTORY_SYNC` gate and `<db>.lock`. It listens to DXLink `TimeAndSale` through the shared streamer, buffers trades, and flushes once a second into two new tables (`ticks`, `live_1m`). Reads merge `bars_1m` (official) with `live_1m` for minutes newer than the newest official minute. End of day runs after the store's 20:15 ET final pass.

**Tech Stack:** Python 3.14, FastAPI, sqlite3 (sync, via `asyncio.to_thread`), tastytrade SDK DXLink (`TimeAndSale`), pytest/pytest-asyncio; React chart (`node --test` for `.test.mjs`).

**Spec:** `docs/superpowers/specs/2026-11-24-live-tick-bars-design.md` (builds on `docs/superpowers/specs/2026-11-20-chart-history-design.md`).

## Global Constraints

- Branch `feat/live-tick-bars` (off `feat/chart-history` 686623b), worktree `/Users/karanpatel/Tradding-system-candles`. Do not push, merge to `main`, reinstall the LaunchAgent or restart the service: `feat/chart-history` is under a project hold; shipping is a separate decision.
- Backend tests: `cd backend && DATABASE_URL=sqlite+aiosqlite:///$TMPDIR/_t_ltb.db ../../Tradding-system/.venv/bin/python -m pytest -q -p no:cacheprovider` (delete the `_t_ltb.db` file before each full run). `MARKET_HISTORY_PATH` defaults to a temp file via `tests/__init__.py`; never point it at `backend/market_history.db`.
- Frontend tests: `node --test frontend/src/components/charts/`.
- RTH = 09:30:00.000–16:00:00.000 America/New_York on days where `app.services.market._is_trading_day` is true. Half days are treated as full days.
- Only the process holding `<market_history_path>.lock` writes `market_history.db`.
- Official bars win: a `live_1m` minute is never written into `bars_1m`, and is never returned for a minute that exists in `bars_1m`.
- No test touches the network, the real `market_history.db`, or `trading.db`.
- Bar shape everywhere: `{"time": epoch ms UTC (minute start), "open", "high", "low", "close", "volume"}`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **DXLink drop mid-session:** after `_drop_state()` the `TimeAndSale` subscription is gone; the recorder must resubscribe within a few seconds (Task 4 test `test_recorder_resubscribes_after_drop`).
2. **Out-of-order / late trades:** a trade for an earlier minute arriving after that minute closed must update that minute, not the forming one (Task 2 test `test_fold_late_trade_updates_its_own_minute`).
3. **Duplicate delivery after reconnect:** the same `(symbol, ts_ms, seq)` twice must not double volume (Task 3 test `test_insert_ticks_ignores_duplicates_and_live_matches_ticks`).
4. **Service down across midnight / Mac asleep at 20:15:** earlier-day `ticks`/`live_1m` must be finalized and deleted at next start (Task 5 test `test_startup_finalizes_earlier_days`).
5. **Flush failure (disk/lock error):** buffered trades are kept for the next flush, the recorder keeps running, nothing raises into the app (Task 4 test `test_flush_failure_keeps_buffer_and_continues`).

---

### Task 1: Gate — confirm `TimeAndSale` arrives for this account

**Files:**
- Create: `backend/scripts/probe_time_and_sale.py`

- [ ] **Step 1: Write the probe**

`python -m scripts.probe_time_and_sale --symbols SPY,QQQ,TSLA,NVDA --seconds 60`: opens a session with `app.tastytrade.session.get_tt_session()`, a `DXLinkStreamer`, subscribes `TimeAndSale` for the symbols, and prints per symbol: event count, count with `extended_trading_hours` true, count with `valid_tick` false, distinct `type` values, and three sample events (`time`, `price`, `size`, `sequence`, `type`). Read-only; no database access.

- [ ] **Step 2: Run it during RTH (09:30–16:00 ET on a trading day)**

Run: `cd backend && ../../Tradding-system/.venv/bin/python -m scripts.probe_time_and_sale --symbols SPY,QQQ,TSLA,NVDA --seconds 60`
Expected: every symbol shows a non-zero count. Record counts (×390 ≈ trades/day) and observed `type` values in the commit message.
If any symbol shows 0 events: **stop the plan** and report back; the fallback (bars from `Trade` + `day_volume` deltas) needs a user decision.

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/probe_time_and_sale.py
git commit -m "chore(history): TimeAndSale probe (<counts per symbol per minute>)"
```

### Task 2: Symbol list and trade-to-minute folding

**Files:**
- Modify: `backend/app/config.py` (next to `market_history_sync`)
- Modify: `backend/app/history/sync.py:22` (`STOCK_SYMBOLS`), `backend/app/history/service.py:16,141` (users of it)
- Create: `backend/app/history/live_bars.py`
- Test: `backend/tests/test_live_bars.py`

**Interfaces:**
- Produces: `Settings.live_bar_symbols: str = "SPY,QQQ,TSLA,NVDA"` (env `LIVE_BAR_SYMBOLS`).
- Produces: `app.history.live_bars.stock_symbols() -> tuple[str, ...]` — parsed, upper-cased, de-duplicated, validated with `chart_data.normalize_symbol`; `SPX`/`NDX` and invalid entries dropped with one warning each. Replaces every use of `STOCK_SYMBOLS` (keep `STOCK_SYMBOLS` out of the code entirely; `sync.py` and `service.py` call `stock_symbols()`).
- Produces: `Trade = NamedTuple("Trade", symbol: str, ts_ms: int, seq: int, price: float, size: float)`.
- Produces: `is_rth_trade(ts_ms: int, extended: bool | None, valid: bool | None) -> bool`.
- Produces: `minute_of(ts_ms: int) -> int` — minute start in ms; a trade at exactly 16:00:00.000 ET maps to the 15:59 minute.
- Produces: `fold(bars: dict[int, dict], trades: Iterable[Trade]) -> set[int]` — mutates `bars` (keyed by minute ms) in trade-time order per minute, returns touched minute keys. Bars carry an extra `"trades"` count.

- [ ] **Step 1: Write the failing tests**

```python
def test_stock_symbols_from_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "live_bar_symbols", "spy, QQQ,SPX,qqq,bad sym,NVDA")
    assert stock_symbols() == ("SPY", "QQQ", "NVDA")

def test_rth_window():
    assert is_rth_trade(_et(9, 30, 0, 0), False, True)
    assert not is_rth_trade(_et(9, 29, 59, 999), False, True)
    assert is_rth_trade(_et(16, 0, 0, 0), False, True)          # closing print kept
    assert not is_rth_trade(_et(16, 0, 0, 1), False, True)
    assert not is_rth_trade(_et(10, 0, 0, 0), True, True)       # extended-hours flag
    assert not is_rth_trade(_et(10, 0, 0, 0), False, False)     # invalid tick
    assert not is_rth_trade(_et(10, 0, 0, 0, day=HOLIDAY), False, True)

def test_closing_print_folds_into_1559():
    assert minute_of(_et(16, 0, 0, 0)) == _et(15, 59, 0, 0)

def test_fold_builds_ohlcv():
    bars = {}
    fold(bars, [T(_et(10,0,1), 1, 100.0, 10), T(_et(10,0,30), 2, 101.0, 5), T(_et(10,0,59), 3, 99.5, 1)])
    assert bars[_et(10,0)] == {"time": _et(10,0), "open": 100.0, "high": 101.0, "low": 99.5,
                               "close": 99.5, "volume": 16.0, "trades": 3}

def test_fold_late_trade_updates_its_own_minute():
    bars = {}
    fold(bars, [T(_et(10,1,5), 2, 101.0, 1)])
    touched = fold(bars, [T(_et(10,0,50), 1, 100.0, 2)])
    assert touched == {_et(10,0)} and bars[_et(10,1)]["volume"] == 1.0 and bars[_et(10,0)]["close"] == 100.0
```

(`_et(h, m, s=0, ms=0, day=<a trading day>)` and `T(ts, seq, price, size, symbol="SPY")` are test helpers in the file; `HOLIDAY = date(2026, 11, 26)`.)

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_live_bars.py -v` → FAIL (module not found).
- [ ] **Step 3: Implement** `live_bars.py`, the setting, and switch `sync.py`/`service.py` to `stock_symbols()`. `fold` with a late trade: open/close follow trade time order — keep per-bar `first_ts`/`last_ts` internally (not returned) so a late trade only changes open if earlier than `first_ts`, close if later than `last_ts`.
- [ ] **Step 4: Run** `pytest tests/test_live_bars.py tests/test_history_sync.py tests/test_history_api.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(history): LIVE_BAR_SYMBOLS list and trade-to-minute folding`.

### Task 3: Store tables for trades and trade-built minutes

**Files:**
- Modify: `backend/app/history/store.py` (`_SCHEMA`, `delete_symbol`, new methods)
- Test: `backend/tests/test_history_store.py` (append)

**Interfaces:**
- Consumes: `Trade`, `fold`, `minute_of` (Task 2).
- Produces on `HistoryStore`:
  - `insert_trades(trades: list[Trade]) -> set[tuple[str, int]]` — one transaction: `INSERT OR IGNORE` into `ticks(symbol, ts_ms, seq, price, size, PRIMARY KEY(symbol, ts_ms, seq))`, then recompute each touched `(symbol, minute)` in `live_1m(symbol, ts, open, high, low, close, volume, trades, PRIMARY KEY(symbol, ts))` **from `ticks`** (so duplicates and late trades are exact). Returns touched `(symbol, minute)`.
  - `live_minutes(symbol: str, start_ms: int, end_ms: int) -> list[dict]` — ascending, bar shape (no `trades` key).
  - `rebuild_live(day: date) -> int` — recompute all `live_1m` rows of that ET date from `ticks`; returns rows written.
  - `live_days() -> list[date]` — distinct ET dates present in `ticks` or `live_1m`.
  - `delete_live_day(day: date) -> None` — delete that ET date from `ticks` and `live_1m` in one transaction.
  - `delete_symbol` also clears `ticks` and `live_1m`.

- [ ] **Step 1: Write the failing tests**

```python
def test_insert_ticks_ignores_duplicates_and_live_matches_ticks(store):
    ts = [T(_et(10,0,1),1,100.0,10), T(_et(10,0,2),2,101.0,5)]
    store.insert_trades(ts); store.insert_trades(ts)                       # redelivery
    assert store.live_minutes("SPY", 0, 2**62) == [
        {"time": _et(10,0), "open": 100.0, "high": 101.0, "low": 100.0, "close": 101.0, "volume": 15.0}]

def test_live_tables_do_not_touch_bars_1m(store):
    store.insert_trades([T(_et(10,0,1),1,100.0,10)])
    assert store.minutes("SPY", 0, 2**62) == []

def test_live_days_and_delete(store):
    store.insert_trades([T(_et(10,0,1, day=D1),1,1.0,1), T(_et(10,0,1, day=D2),1,1.0,1)])
    assert store.live_days() == [D1, D2]
    store.delete_live_day(D1)
    assert store.live_days() == [D2]

def test_rebuild_live_from_ticks(store):
    store.insert_trades([T(_et(10,0,1),1,100.0,10)])
    with closing(store._connect()) as c, c: c.execute("DELETE FROM live_1m")
    assert store.rebuild_live(D_TODAY) == 1 and len(store.live_minutes("SPY", 0, 2**62)) == 1
```

- [ ] **Step 2: Run** `pytest tests/test_history_store.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.** Recompute a minute in SQL (`MIN/MAX`, first/last price by `ts_ms, seq` order, `SUM(size)`, `COUNT(*)`), not from in-memory state.
- [ ] **Step 4: Run** `pytest tests/test_history_store.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(history): ticks and live_1m tables`.

### Task 4: Streamer `TimeAndSale` support and the recorder

**Files:**
- Modify: `backend/app/tastytrade/streamer.py` (`_subscribed` init in `__init__` and `_drop_state`, `_start_pumps`, `_pump`)
- Create: `backend/app/history/recorder.py`
- Modify: `backend/app/history/sync.py` (`run_forever`: start the recorder after the lock is acquired, cancel it in `finally`)
- Test: `backend/tests/test_tt_streamer.py` (append), `backend/tests/test_history_recorder.py`

**Interfaces:**
- Produces on `TTStreamer`: `"TimeAndSale"` key in `_subscribed`; a `TimeAndSale` pump; `add_listener(cls_name: str, fn: Callable[[Any], None]) -> None` and `remove_listener(cls_name, fn)`; the pump calls each listener synchronously per event and logs (never propagates) listener exceptions; `async _unsubscribe(cls, symbols: list[str]) -> None` (best-effort `dx.unsubscribe`, removes them from `_subscribed`, never raises). The recorder calls `_unsubscribe(TimeAndSale, …)` when RTH ends.
- Produces: `class LiveRecorder(store: HistoryStore, streamer_factory=get_streamer, now=lambda: datetime.now(UTC), flush_seconds: float = 1.0)` with
  - `async run() -> None` — forever: outside RTH hold no subscription and sleep until 09:29:30 ET of the next trading day (wake at least every 60 s); inside RTH ensure listener + `TimeAndSale` subscription for `stock_symbols()` (re-check every flush: resubscribe if the symbol is missing from `streamer._subscribed["TimeAndSale"]`), flush every `flush_seconds`.
  - `on_event(ev) -> None` — converts to `Trade` (`ev.event_symbol`, `int(ev.time)`, `int(ev.sequence or 0)`, `float(ev.price)`, `float(ev.size or 0)`), keeps only listed symbols where `is_rth_trade(...)`, appends to the buffer, folds into the in-memory forming bars.
  - `async flush() -> int` — swaps the buffer, `await asyncio.to_thread(store.insert_trades, batch)`; on exception puts the batch back at the front, logs, returns 0.
  - `live_bars(symbol: str) -> list[dict]` — the just-closed and forming minute for today from memory (0–2 bars, bar shape), empty outside RTH.
  - On `run()` start during RTH: `await asyncio.to_thread(store.rebuild_live, today)` and seed in-memory bars for the current and previous minute from `store.live_minutes`.
- Produces: module-level `recorder: LiveRecorder | None` set by `run_forever` (read by Task 6).

- [ ] **Step 1: Write the failing tests** (fake streamer with `_subscribed`, `_subscribe`, `add_listener`; fixed `now`)

```python
async def test_recorder_records_rth_trades_and_flushes(store, fake_streamer):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30))
    rec.on_event(_tas("SPY", _et(10,0,1), 1, 100.0, 10))
    rec.on_event(_tas("SPY", _et(10,0,2), 2, 100.0, 1, extended=True))     # dropped
    rec.on_event(_tas("AAPL", _et(10,0,2), 1, 1.0, 1))                     # not listed
    assert await rec.flush() == 1
    assert store.live_minutes("SPY", 0, 2**62)[0]["volume"] == 10.0
    assert rec.live_bars("SPY")[-1]["close"] == 100.0

async def test_flush_failure_keeps_buffer_and_continues(store, fake_streamer, monkeypatch):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30))
    rec.on_event(_tas("SPY", _et(10,0,1), 1, 100.0, 10))
    monkeypatch.setattr(store, "insert_trades", _raise(OSError("disk full")))
    assert await rec.flush() == 0
    monkeypatch.undo()
    assert await rec.flush() == 1

async def test_recorder_resubscribes_after_drop(store, fake_streamer):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30), flush_seconds=0.01)
    task = asyncio.create_task(rec.run()); await asyncio.sleep(0.05)
    fake_streamer._subscribed["TimeAndSale"].clear()                         # DXLink drop
    await asyncio.sleep(0.05); task.cancel()
    assert set(stock_symbols()) <= fake_streamer._subscribed["TimeAndSale"]

async def test_no_subscription_outside_rth(store, fake_streamer):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(17, 0, 0), flush_seconds=0.01)
    task = asyncio.create_task(rec.run()); await asyncio.sleep(0.05); task.cancel()
    assert not fake_streamer._subscribed["TimeAndSale"]

async def test_unsubscribes_when_rth_ends(store, fake_streamer):
    clock = [_dt(15, 59, 59)]
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: clock[0], flush_seconds=0.01)
    task = asyncio.create_task(rec.run()); await asyncio.sleep(0.05)
    clock[0] = _dt(16, 0, 1); await asyncio.sleep(0.05); task.cancel()
    assert not fake_streamer._subscribed["TimeAndSale"]
```

Streamer test: `test_time_and_sale_listener_receives_events` (push a fake `TimeAndSale` event through `_FakeDx`; listener called once; a raising listener does not kill the pump).

- [ ] **Step 2: Run** `pytest tests/test_history_recorder.py tests/test_tt_streamer.py -v` → FAIL.
- [ ] **Step 3: Implement** streamer changes, `recorder.py`, and the `run_forever` wiring.
- [ ] **Step 4: Run** `pytest tests/test_history_recorder.py tests/test_tt_streamer.py tests/test_history_sync.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(history): record TimeAndSale trades into 1-minute bars during RTH`.

### Task 5: End of day — finalize and delete

**Files:**
- Modify: `backend/app/history/sync.py` (`HistorySync.finalize_day`, call sites in `run_once`)
- Test: `backend/tests/test_history_sync.py` (append)

**Interfaces:**
- Consumes: `store.live_days`, `store.delete_live_day`, `store.minutes`, `HistorySync._pull` (existing), `stock_symbols()`.
- Produces: `HistorySync.finalize_day(day: date) -> None` — for each listed symbol, find RTH minutes of `day` (09:30…15:59) missing from `bars_1m`; if any, `_pull` that day's 09:30–16:00 range once with `feed="sip"` and write via `_write_chunk`; minutes still missing are recorded as `sync_state.note = "<n> minutes missing on <day>"` (no status change). Then `delete_live_day(day)`.
- `run_once` calls `finalize_day(d)` for every `d` in `live_days()` where `d < today_et`, or `d == today_et` and ET time ≥ 20:15. It runs after the per-symbol sync loop; an exception in `finalize_day` is logged and the day is retried next pass (not deleted).

- [ ] **Step 1: Write the failing tests**

```python
async def test_finalize_deletes_trades_when_day_complete(sync, store):     # fake fetch returns nothing
    _fill_rth_minutes(store, "SPY", D1); store.insert_trades([T(_et(10,0,1, day=D1),1,1.0,1)])
    await sync.finalize_day(D1)
    assert store.live_days() == []

async def test_finalize_refetches_missing_once_and_notes_gaps(sync, store, fake_fetch):
    _fill_rth_minutes(store, "SPY", D1, skip={_et(11,0, day=D1)})
    await sync.finalize_day(D1)
    assert fake_fetch.calls == [("SPY", D1)] and "1 minutes missing" in store.get_state("SPY")["note"]

async def test_startup_finalizes_earlier_days(sync, store):               # now = D2 10:00 ET
    store.insert_trades([T(_et(10,0,1, day=D1),1,1.0,1)])
    await sync.run_once()
    assert D1 not in store.live_days()

async def test_today_not_finalized_before_2015(sync, store):              # now = D2 19:00 ET
    store.insert_trades([T(_et(10,0,1, day=D2),1,1.0,1)])
    await sync.run_once()
    assert store.live_days() == [D2]
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `pytest tests/test_history_sync.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(history): end-of-day finalize and trade deletion`.

### Task 6: Serve trade-built minutes (history API and `/ws`)

**Files:**
- Modify: `backend/app/history/service.py` (`_back`, `_forward`, `get_history`, `_result`)
- Modify: `backend/app/services/chart_live.py` (`prices` → also produce bars), `backend/app/routers/ws.py:151` (snapshot field)
- Test: `backend/tests/test_history_api.py`, `backend/tests/test_chart_live.py` (append)

**Interfaces:**
- Consumes: `store.live_minutes`, `recorder.live_bars` (Task 4), module `app.history.recorder.recorder`.
- Produces: in `service.py`, a helper `_minutes(store, symbol, start_ms, end_ms, official_latest: int) -> list[dict]` used by `_back`/`_forward` instead of `store.minutes`: official minutes plus `live_minutes` with `time > official_latest`, merged ascending. The stock path's upper bound `hi` becomes `max(official hi, newest live minute)`.
- Produces: `_result(...)` gains `live_from: int | None` — time of the first trade-built minute in the response's underlying minutes (None if none).
- `1D` for a listed symbol during RTH: the last daily bar for today is replaced by one built from today's merged minutes (09:30–15:59) when today's minutes include any live minute.
- Produces: snapshot field `chart_bars: {symbol: [bar, ...]}` from `ChartLive.bars(conn_id)` — for chart symbols in `stock_symbols()`, `recorder.live_bars(symbol)` during the regular session; `{}` otherwise. `chart_prices` unchanged (still sent for all chart symbols).

- [ ] **Step 1: Write the failing tests**

```python
async def test_live_minutes_fill_after_official(store):
    _put_official(store, "SPY", [_et(10,0), _et(10,1)]); _put_live(store, "SPY", [_et(10,1), _et(10,2)])
    out = await get_history("SPY", "1m")
    assert [b["time"] for b in out["bars"]][-3:] == [_et(10,0), _et(10,1), _et(10,2)]
    assert out["bars"][-2]["close"] == OFFICIAL_CLOSE and out["live_from"] == _et(10,2)

async def test_official_wins_when_both_exist(store):
    _put_official(store, "SPY", [_et(10,0)]); _put_live(store, "SPY", [_et(10,0)])
    out = await get_history("SPY", "1m")
    assert out["bars"][-1]["close"] == OFFICIAL_CLOSE and out["live_from"] is None

async def test_5m_bucket_spans_official_and_live(store):
    _put_official(store, "SPY", [_et(10,0), _et(10,1)]); _put_live(store, "SPY", [_et(10,2)])
    out = await get_history("SPY", "5m")
    assert out["bars"][-1]["time"] == _et(10,0) and out["bars"][-1]["volume"] == 3 * VOL

def test_chart_bars_only_for_listed_symbols_in_rth(fake_recorder): ...
    # SPY in chart symbols during "regular" → chart_bars["SPY"] == fake_recorder.live_bars("SPY");
    # AAPL → absent; session "after_hours" → {}
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `pytest tests/test_history_api.py tests/test_chart_live.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(history): serve trade-built minutes after the newest official bar`.

### Task 7: Chart applies server-built live bars

**Files:**
- Modify: `frontend/src/components/charts/liveCandle.js`, `frontend/src/components/charts/ChartWorkspace.jsx:~95-110`, `frontend/src/hooks/useWebSocket.js` (expose `chart_bars`)
- Test: `frontend/src/components/charts/liveCandle.test.mjs` (append)

**Interfaces:**
- Consumes: snapshot `chart_bars` (Task 6).
- Produces: `applyLiveBars(bars, liveBars, interval) -> bars` in `liveCandle.js` — for `interval === "1m"`: each live bar replaces the bar with the same `time` or is appended if newer than the last; older ones are ignored; returns `bars` itself when nothing changes. For other intervals: returns `foldForInterval(bars, last.close, last.time + 59_999, interval)` using the newest live bar (price-only fold, as today; volume for those intervals comes from the history re-poll).
- `ChartWorkspace.jsx`: when `chart_bars[symbol]` exists use `applyLiveBars`; otherwise keep the existing `chart_prices` fold.
- Coordinate with the FRONTEND session before editing these files (it owns the chart UI); tell it which lines change.

- [ ] **Step 1: Write the failing tests**

```js
test("1m: live bar replaces same-minute bar, appends newer, ignores older", () => {
  const out = applyLiveBars([b(0, 1), b(60e3, 2)], [b(60e3, 3), b(120e3, 4)], "1m");
  assert.deepEqual(out.map(x => [x.time, x.close]), [[0, 1], [60e3, 3], [120e3, 4]]);
  assert.equal(applyLiveBars(out, [b(0, 9)], "1m"), out);
});
test("5m: folds newest live close into the last bucket", () => {
  const out = applyLiveBars([b(0, 1)], [b(120e3, 5)], "5m");
  assert.equal(out.at(-1).close, 5); assert.equal(out.length, 1);
});
```

- [ ] **Step 2: Run** `node --test frontend/src/components/charts/` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; also `cd frontend && npx vite build` succeeds.
- [ ] **Step 5: Commit** `feat(chart): apply server-built live 1-minute bars`.

### Task 8: Full verification and docs

**Files:**
- Modify: `README.md` (chart history section: live recorder, `LIVE_BAR_SYMBOLS`, end-of-day deletion)

- [ ] **Step 1:** Full backend suite (command in Global Constraints) → all pass; `node --test frontend/src/components/charts/` → all pass.
- [ ] **Step 2:** Local smoke run (not the service): `MARKET_HISTORY_PATH=$TMPDIR/ltb_smoke.db MARKET_HISTORY_SYNC=true DATABASE_URL=sqlite+aiosqlite:///$TMPDIR/ltb_smoke_trading.db uvicorn app.main:app --port 8799` during RTH for 3 minutes (the Alpaca backfill also starts into the smoke file; that is expected); then `sqlite3 $TMPDIR/ltb_smoke.db "select symbol,count(*) from live_1m group by 1"` shows rows for each listed symbol and `GET /api/market/history?symbol=SPY&interval=1m` returns a non-null `live_from`. Stop the server and delete the smoke files.
- [ ] **Step 3:** README update; commit `docs: live trade recorder`.
