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

# NYSE full-day closures for 2026 (early-close half days are treated as open).
NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}

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
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS_2026


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
# hot path — the dashboard polls every 15s and we don't want to re-pull two weeks
# of candles each time.
_WEEKLY_TTL = 600.0  # seconds
_weekly_cache: dict[str, tuple[float | None, float]] = {}
_WEEKLY_CONCURRENCY = asyncio.Semaphore(3)


def cached_week_change(sym: str) -> float | None:
    """Synchronous read of the cached weekly % for a symbol (None if absent/stale).
    Used by the live stream's snapshot builder, which must not block on the feed."""
    cached = _weekly_cache.get(sym)
    if cached and monotonic() - cached[1] < _WEEKLY_TTL:
        return cached[0]
    return None


async def _weekly_changes(symbols: list[str]) -> dict[str, float | None]:
    """5-trading-day % change per symbol from daily candles, cached for ``_WEEKLY_TTL``.

    Best-effort: a symbol with insufficient bars (or a feed hiccup) maps to None.
    Only cache-stale symbols hit the feed, throttled by ``_WEEKLY_CONCURRENCY``."""
    now = monotonic()
    result: dict[str, float | None] = {}
    stale: list[str] = []
    for sym in symbols:
        cached = _weekly_cache.get(sym)
        if cached and now - cached[1] < _WEEKLY_TTL:
            result[sym] = cached[0]
        else:
            stale.append(sym)

    if not stale:
        return result

    start = datetime.now(timezone.utc) - timedelta(days=16)
    from app.providers import get_historical_provider
    provider = get_historical_provider()

    async def one(sym: str) -> tuple[str, float | None]:
        try:
            async with _WEEKLY_CONCURRENCY:
                bars = await provider.get_bars(sym, "1d", start=start)
            closes = [b["close"] for b in bars if b["close"]]
            if len(closes) < 6:
                return sym, None
            ref, last = closes[-6], closes[-1]  # ~5 trading days back
            return sym, round(last / ref - 1.0, 4) if ref else None
        except Exception:  # noqa: BLE001
            return sym, None

    for sym, val in await asyncio.gather(*(one(s) for s in stale)):
        _weekly_cache[sym] = (val, now)
        result[sym] = val
    return result
