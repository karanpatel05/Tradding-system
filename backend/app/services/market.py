"""Market status + overview.

Market open/closed is computed locally from the US/Eastern clock and the NYSE
holiday calendar — it needs no connection and is always available. The overview
(index quotes, weekly performance) pulls live data from IBKR when connected and
**degrades gracefully** to an "unavailable" state otherwise.

NOTE: IBKR market data for cash indices needs index-data entitlements, so the
overview uses liquid **ETF proxies** (SPY/QQQ/DIA/IWM/VIXY) via ordinary stock
quotes. Values track the indices but are ETF prices, not cash-index levels.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from app.marketdata.facade import stock_quotes as facade_stock_quotes

logger = logging.getLogger("trading.market")

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PREMARKET_OPEN = time(4, 0)
AFTERHOURS_CLOSE = time(20, 0)

# NYSE full-day closures (early-close half days are treated as open). Verified against
# pandas_market_calendars. Add the next year before it starts: an uncovered year logs a
# warning and treats every weekday as a trading day.
NYSE_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
_HOLIDAY_YEARS = {d.year for d in NYSE_HOLIDAYS}
_warned_years: set[int] = set()

# Liquid ETF proxies for the market overview (IBKR stock quotes). ``featured`` marks
# the headline ticker; ``kind`` distinguishes a volatility gauge from a price index
# so the UI can format it differently.
INDICES = [
    {"symbol": "SPY", "name": "S&P 500", "kind": "index", "featured": True},
    {"symbol": "QQQ", "name": "Nasdaq 100", "kind": "index"},
    {"symbol": "DIA", "name": "Dow Jones", "kind": "index"},
    {"symbol": "IWM", "name": "Russell 2000", "kind": "index"},
    {"symbol": "VIXY", "name": "Volatility", "kind": "volatility"},
]

# Fields surfaced per index when no live data is available (keeps the JSON shape
# stable so the UI renders placeholders rather than crashing).
_NULL_QUOTE = {"last": None, "change": None, "change_pct": None,
               "week_change_pct": None, "prev_close": None}


def index_row(ix: dict, quote: dict | None, week_change: float | None = None) -> dict:
    """Build one overview row from a normalized IBKR quote ({last, close, ...}).
    Day change is derived from last vs the prior close the ticker carries."""
    last = (quote or {}).get("last")
    prev = (quote or {}).get("close")
    change = round(last - prev, 4) if last is not None and prev not in (None, 0) else None
    change_pct = round(change / prev, 4) if change is not None and prev else None
    return {**ix, "last": last, "change": change, "change_pct": change_pct,
            "prev_close": prev, "day_volume": (quote or {}).get("day_volume"),
            "week_change_pct": week_change if week_change is not None
            else cached_week_change(ix["symbol"])}


def _is_trading_day(d: date) -> bool:
    if d.year not in _HOLIDAY_YEARS and d.year not in _warned_years:
        _warned_years.add(d.year)
        logger.warning("NYSE holiday calendar has no entries for %d; holidays will show "
                       "as trading days until NYSE_HOLIDAYS in app/services/market.py "
                       "is updated", d.year)
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def get_market_status() -> dict:
    now = datetime.now(ET)
    today = now.date()
    t = now.time()
    trading_day = _is_trading_day(today)

    if not trading_day:
        session = "closed"
    elif REGULAR_OPEN <= t < REGULAR_CLOSE:
        session = "regular"
    elif PREMARKET_OPEN <= t < REGULAR_OPEN:
        session = "pre_market"
    elif REGULAR_CLOSE <= t < AFTERHOURS_CLOSE:
        session = "after_hours"
    else:
        session = "closed"

    return {
        "is_open": session == "regular",
        "session": session,  # regular | pre_market | after_hours | closed
        "is_trading_day": trading_day,
        "now_et": now.isoformat(),
        "regular_open_et": REGULAR_OPEN.strftime("%H:%M"),
        "regular_close_et": REGULAR_CLOSE.strftime("%H:%M"),
    }


async def get_market_overview() -> dict:
    """Live index snapshot + weekly performance. Degrades gracefully."""
    status = get_market_status()
    base = {"status": status, "indices": [], "data_available": False, "note": None}

    symbols = [ix["symbol"] for ix in INDICES]
    quotes, week_changes = await asyncio.gather(
        facade_stock_quotes(symbols, timeout=2.5),
        _weekly_changes(symbols),
        return_exceptions=True,
    )
    # BaseException, not Exception: a cancelled child comes back as CancelledError.
    if isinstance(quotes, BaseException):  # overview must never 500
        logger.warning("Overview quote fetch failed: %s", quotes)
        base["note"] = "Market data feed temporarily unavailable."
        base["indices"] = [{**ix, **_NULL_QUOTE} for ix in INDICES]
        return base
    if isinstance(week_changes, BaseException):  # weekly is best-effort, never fatal
        logger.warning("Weekly change fetch failed: %s", week_changes)
        week_changes = {}

    if quotes and all(q.get("price") is None for q in quotes.values()):
        base["note"] = "No market data source available (Tastytrade not configured, Gateway down)."

    indices = [index_row(ix, quotes.get(ix["symbol"]), week_changes.get(ix["symbol"]))
               for ix in INDICES]
    base["indices"] = indices
    base["data_available"] = any(ix["last"] is not None for ix in indices)
    if not base["data_available"]:
        base["note"] = "Index feed returned no data (markets may be closed)."
    return base


# Weekly % change moves only on the daily close, so it's cached to keep it off the
# hot path. Candle collection is serialized and takes seconds per symbol, so a refresh
# NEVER runs inline: readers get the cached value (stale included) immediately and at
# most one background task refreshes the whole set. Inline refreshes used to block the
# overview for 20s+ and, with no dedupe, every concurrent poll queued its own five
# fetches in front of the charts and strategies that share the candle collector.
_WEEKLY_TTL = 600.0  # seconds
_weekly_cache: dict[str, tuple[float | None, float]] = {}
_WEEKLY_CONCURRENCY = 3  # parallel candle requests within one refresh
_weekly_task: asyncio.Task | None = None  # strong ref: asyncio only holds tasks weakly


def _weekly_stale(symbols: list[str], now: float) -> list[str]:
    return [s for s in symbols
            if (c := _weekly_cache.get(s)) is None or now - c[1] >= _WEEKLY_TTL]


def _schedule_weekly_refresh(symbols: list[str]) -> None:
    """Start the background refresh unless one is already running (or no loop is)."""
    global _weekly_task
    if _weekly_task is not None and not _weekly_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _weekly_task = loop.create_task(_refresh_weekly(symbols), name="weekly-change-refresh")


def cached_week_change(sym: str) -> float | None:
    """Synchronous read of the cached weekly % for a symbol (stale values included;
    None if never fetched). Used by the live stream's snapshot builder, which must not
    block on the feed; a stale or missing entry kicks the single background refresh."""
    if _weekly_stale([sym], monotonic()):
        _schedule_weekly_refresh([ix["symbol"] for ix in INDICES])
    cached = _weekly_cache.get(sym)
    return cached[0] if cached else None


async def _weekly_changes(symbols: list[str]) -> dict[str, float | None]:
    """5-trading-day % change per symbol, from cache only (never waits on the feed).

    Stale or missing symbols trigger one background refresh; until it lands they keep
    their last value, or None if never fetched."""
    if _weekly_stale(symbols, monotonic()):
        _schedule_weekly_refresh(symbols)
    return {s: (c[0] if (c := _weekly_cache.get(s)) else None) for s in symbols}


def _history_provider():
    """Daily-candle source for the weekly refresh (a seam tests replace)."""
    from app.providers import get_historical_provider
    return get_historical_provider()


async def _refresh_weekly(symbols: list[str]) -> None:
    """Fetch daily candles for the stale symbols and update the cache. Best-effort:
    a symbol with insufficient bars or a feed error maps to None; never raises."""
    try:
        stale = _weekly_stale(symbols, monotonic())
        if not stale:
            return
        start = datetime.now(timezone.utc) - timedelta(days=16)
        provider = _history_provider()
        gate = asyncio.Semaphore(_WEEKLY_CONCURRENCY)  # per refresh: binds to this loop

        async def one(sym: str) -> tuple[str, float | None]:
            try:
                async with gate:
                    bars = await provider.get_bars(sym, "1d", start=start)
                closes = [b["close"] for b in bars if b["close"]]
                if len(closes) < 6:
                    return sym, None
                ref, last = closes[-6], closes[-1]  # ~5 trading days back
                return sym, round(last / ref - 1.0, 4) if ref else None
            except Exception:  # noqa: BLE001
                return sym, None

        for sym, val in await asyncio.gather(*(one(s) for s in stale)):
            _weekly_cache[sym] = (val, monotonic())
    except Exception:  # noqa: BLE001 - background task: log, never leak
        logger.warning("Weekly change refresh failed", exc_info=True)
