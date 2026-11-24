"""History read service behind GET /api/market/history.

Stocks (SPY/QQQ/TSLA/NVDA) read the local SQLite store only; SPX/NDX intraday come from
a 14-day in-memory slice of the Tastytrade 1m candles, SPX/NDX 1D/1W from the existing
chart-data daily path. Pages are cursor based: newest, `before` a bar time, `after` a
bar time, or `around` a date (snapped to the nearest available bars).
"""
import asyncio
import math
import time
from datetime import date, datetime, timedelta, timezone

from app.history.aggregate import (
    ET, INTRADAY_MINUTES, bucket, daily_from_minutes, et_session_start_ms,
    weekly_from_daily)
from app.history.store import get_store
from app.history.live_bars import stock_symbols
from app.marketdata import facade
from app.services import chart_data

INDEX_SYMBOLS = ("SPX", "NDX")
HISTORY_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1D", "1W")
DEFAULT_LIMIT = 600
MAX_LIMIT = 2000
COARSE_MAX_LIMIT = 600  # 1h/4h pages read ~60-240 minutes per bar: cap the walk

INDEX_LOOKBACK_DAYS = 14
INDEX_TTL_SECONDS = 600.0
_MINUTE_MS = 60_000
_SESSION_MINUTES_PER_DAY = 16 * 60  # stored minutes run 04:00-20:00 ET

_index_cache: dict[str, tuple[float, list[dict]]] = {}
_index_locks: dict[str, asyncio.Lock] = {}


class HistoryRequestError(ValueError):
    """Bad history request (symbol, interval, limit or cursor) -> HTTP 400."""


def _midnight_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


def _et_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, ET).date()


# --- page selection over an ascending bar list -------------------------------------
def _around_slice(bars: list[dict], target: int, limit: int) -> list[dict]:
    left = [b for b in bars if b["time"] < target]
    right = [b for b in bars if b["time"] >= target]
    lcount = min(len(left), limit // 2)
    rcount = min(len(right), limit - lcount)
    lcount = min(len(left), limit - rcount)  # one side short: fill from the other
    return (left[len(left) - lcount:] if lcount else []) + right[:rcount]


def _select(bars: list[dict], before, after, around, limit) -> list[dict]:
    if before is not None:
        return [b for b in bars if b["time"] < before][-limit:]
    if after is not None:
        return [b for b in bars if b["time"] > after][:limit]
    if around is not None:
        return _around_slice(bars, et_session_start_ms(around), limit)
    return bars[-limit:]


# --- stock intraday: chunked reads of the minute table -----------------------------
def _chunk_days(interval: str, limit: int) -> int:
    trading_days = limit * INTRADAY_MINUTES[interval] / _SESSION_MINUTES_PER_DAY
    return max(2, math.ceil(trading_days * 7 / 5) + 1)


def _today_start_ms() -> int:
    return _midnight_ms(datetime.now(ET).date())


def _live_after(store, symbol: str, official_latest: int) -> list[dict]:
    """Trade-built minutes of today newer than the newest official minute (ascending).
    Official bars always win: a live minute at or before `official_latest` is never used."""
    return store.live_minutes(symbol, max(official_latest + 1, _today_start_ms()), 2**62)


def _minutes(store, symbol, start_ms, end_ms, official_latest: int) -> list[dict]:
    """Official 1m bars in [start_ms, end_ms) plus today's trade-built minutes newer than
    `official_latest`, ascending. Synchronous."""
    out = store.minutes(symbol, start_ms, end_ms)
    live_start = max(start_ms, official_latest + 1, _today_start_ms())
    if live_start < end_ms:
        out = out + store.live_minutes(symbol, live_start, end_ms)
    return out


def _back(store, symbol, interval, end_ms, limit, earliest, latest, official_latest) -> list[dict]:
    """Up to `limit` buckets of minutes before `end_ms` (a bucket boundary), ascending.
    Chunks start at ET midnight, so no bucket is split between two reads. `end_ms` is
    clamped to the stored range so empty time is never walked chunk by chunk.
    Synchronous (reads + bucketing): run the whole walk in one worker thread."""
    step = _chunk_days(interval, limit)
    end_ms = min(end_ms, latest + 1)
    out: list[dict] = []
    while len(out) < limit and end_ms > earliest:
        start = _midnight_ms(_et_date(end_ms - 1) - timedelta(days=step - 1))
        out = bucket(_minutes(store, symbol, start, end_ms, official_latest), interval) + out
        end_ms = start
    return out[-limit:]


def _forward(store, symbol, interval, start_ms, limit, earliest, latest, official_latest) -> list[dict]:
    """Up to `limit` buckets starting at/after `start_ms` (a bucket boundary). Synchronous."""
    step = _chunk_days(interval, limit)
    start_ms = max(start_ms, _midnight_ms(_et_date(earliest)))
    out: list[dict] = []
    while len(out) < limit and start_ms <= latest:
        end = _midnight_ms(_et_date(start_ms) + timedelta(days=step))
        out += bucket(_minutes(store, symbol, start_ms, end, official_latest), interval)
        start_ms = end
    return out[:limit]


async def _stock_intraday(symbol, interval, before, after, around, limit, lo, hi, official_hi):
    store = get_store()
    walk = asyncio.to_thread  # each read+bucket walk runs off the event loop in one call
    n_ms = INTRADAY_MINUTES[interval] * _MINUTE_MS
    if before is not None:
        return await walk(_back, store, symbol, interval, before, limit, lo, hi, official_hi)
    if after is not None:
        return await walk(_forward, store, symbol, interval, after + n_ms, limit, lo, hi, official_hi)
    if around is not None:
        target = et_session_start_ms(around)
        t0 = _latest_bucket_time(target, interval)  # bucket start: 09:30 is mid-bucket at 1h/4h
        left = await walk(_back, store, symbol, interval, t0, limit, lo, hi, official_hi)
        right = await walk(_forward, store, symbol, interval, t0, limit, lo, hi, official_hi)
        return _around_slice(left + right, target, limit)
    return await walk(_back, store, symbol, interval, hi + 1, limit, lo, hi, official_hi)


# --- index sources ------------------------------------------------------------------
async def _index_minutes(symbol: str) -> list[dict]:
    """~14 days of 1m candles from the Tastytrade collector, cached for 10 minutes."""
    hit = _index_cache.get(symbol)
    if hit and time.monotonic() - hit[0] < INDEX_TTL_SECONDS:
        return hit[1]
    async with _index_locks.setdefault(symbol, asyncio.Lock()):
        hit = _index_cache.get(symbol)
        if hit and time.monotonic() - hit[0] < INDEX_TTL_SECONDS:
            return hit[1]
        start = datetime.now(timezone.utc) - timedelta(days=INDEX_LOOKBACK_DAYS)
        raw = await facade.bars(symbol, "1m", start)
        by_time = {int(b["time"]): {**b, "time": int(b["time"])} for b in raw or []}
        minutes = [by_time[t] for t in sorted(by_time)]
        if minutes:  # an empty answer usually means the feed is reconnecting: retry next call
            _index_cache[symbol] = (time.monotonic(), minutes)
        return minutes


def _validate(symbol, interval, before, after, around, limit) -> str:
    try:
        symbol = chart_data.normalize_symbol(symbol)
    except chart_data.ChartDataError as exc:
        raise HistoryRequestError(str(exc)) from exc
    if symbol not in stock_symbols() + INDEX_SYMBOLS:
        raise HistoryRequestError(f"No history for symbol {symbol!r}")
    if interval not in HISTORY_INTERVALS:
        raise HistoryRequestError(f"Unsupported interval: {interval!r}")
    if not 1 <= limit <= MAX_LIMIT:
        raise HistoryRequestError(f"limit must be between 1 and {MAX_LIMIT}")
    if sum(x is not None for x in (before, after, around)) > 1:
        raise HistoryRequestError("Use only one of before, after, around")
    return symbol


def _result(symbol, interval, bars, earliest, latest, latest_bar_time, source, note,
            live_from=None) -> dict:
    return {
        "symbol": symbol, "interval": interval, "bars": bars,
        "earliest": earliest, "latest": latest,
        "has_more_before": bool(bars) and earliest is not None and earliest < bars[0]["time"],
        "has_more_after": bool(bars) and latest_bar_time is not None
        and latest_bar_time > bars[-1]["time"],
        "source": source, "note": note, "live_from": live_from,
    }


def _with_live_day(store, symbol, series: list[dict], live: list[dict], official_hi: int):
    """Replace/append today's daily bar with one built from today's official + live minutes
    (09:30-15:59 ET). Returns (series, time of the first live minute that fed it | None)."""
    today = _today_start_ms()
    merged = store.minutes(symbol, today, official_hi + 1) + live
    built = daily_from_minutes(merged)
    if not built:
        return series, None
    bar = built[-1]
    fed = next((m["time"] for m in live if daily_from_minutes([m])), None)
    rest = [b for b in series if b["time"] != bar["time"]]
    return sorted(rest + [bar], key=lambda b: b["time"]), fed


def _latest_bucket_time(latest: int | None, interval: str) -> int | None:
    if latest is None:
        return None
    return bucket([{"time": latest, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}],
                  interval)[0]["time"]


async def get_history(symbol: str, interval: str, *, before: int | None = None,
                      after: int | None = None, around: date | None = None,
                      limit: int = DEFAULT_LIMIT) -> dict:
    symbol = _validate(symbol, interval, before, after, around, limit)
    if interval in ("1h", "4h"):
        limit = min(limit, COARSE_MAX_LIMIT)
    intraday = interval in INTRADAY_MINUTES
    is_index = symbol in INDEX_SYMBOLS
    live_from = None

    if is_index and not intraday:
        got = await chart_data.get_bars(symbol, "1d")
        series, source, note = got.get("bars") or [], got.get("source") or "tastytrade", got.get("note")
        if interval == "1W":
            series = weekly_from_daily(series)
    elif is_index:
        minutes = await _index_minutes(symbol)
        series = bucket(minutes, interval)
        source = "tastytrade"
        note = None if minutes else "No intraday data from the feed yet."
        lo = minutes[0]["time"] if minutes else None
        hi = minutes[-1]["time"] if minutes else None
        bars = _select(series, before, after, around, limit)
        return _result(symbol, interval, bars, lo, hi, _latest_bucket_time(hi, interval),
                       source, note)
    else:
        store = get_store()
        lo, hi = await asyncio.to_thread(store.bounds, symbol)
        source, note = "alpaca", None
        if lo is None:
            return _result(symbol, interval, [], None, None, None, source,
                           "No local history for this symbol yet.")
        live = await asyncio.to_thread(_live_after, store, symbol, hi)
        official_hi = hi
        if live:
            hi = max(hi, live[-1]["time"])
        if intraday:
            bars = await _stock_intraday(symbol, interval, before, after, around, limit,
                                         lo, hi, official_hi)
            span = INTRADAY_MINUTES[interval] * _MINUTE_MS
            live_from = next((m["time"] for m in live if bars
                              and bars[0]["time"] <= m["time"] < bars[-1]["time"] + span), None)
            return _result(symbol, interval, bars, lo, hi, _latest_bucket_time(hi, interval),
                           source, note, live_from)
        series = await asyncio.to_thread(store.daily, symbol, 0, 2**62)
        if live:
            series, live_from = await asyncio.to_thread(
                _with_live_day, store, symbol, series, live, official_hi)
        if interval == "1W":
            series = weekly_from_daily(series)

    # Daily/weekly series are small enough to page in memory; their bounds are bar times.
    bars = _select(series, before, after, around, limit)
    first = series[0]["time"] if series else None
    last = series[-1]["time"] if series else None
    if live_from is not None and not any(b["time"] == last for b in bars):
        live_from = None  # the page does not include today's (or today's week's) bar
    return _result(symbol, interval, bars, first, last, last, source, note, live_from)
