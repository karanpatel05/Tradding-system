"""Market status + overview.

Market open/closed is computed locally from the US/Eastern clock and the NYSE
holiday calendar — it needs no auth and is always available. The overview (index
quotes, weekly performance) pulls live data from DXFeed when authenticated and
**degrades gracefully** to an "unavailable" state otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from app.auth.manager import session_manager
from app.tastytrade.client import get_client

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

# Cash-index streamer symbols used for the market overview. These are the real
# index levels (not ETF proxies). ``featured`` marks the headline ticker; ``kind``
# distinguishes a volatility gauge (VIX) from a price index so the UI can format it
# differently (no "$", inverted risk colouring is the UI's call).
#
# SPX and VIX are the reliable anchors. The Dow is the awkward one: the DJIA is a
# proprietary index and its DXFeed streamer symbol varies by entitlement, so the
# Dow entry lists several ``candidates`` and we use the first that actually streams
# a correct-magnitude level (``min_level`` rejects e.g. DJX, which is DJIA/100).
INDICES = [
    {"symbol": "SPX", "name": "S&P 500", "kind": "index", "featured": True},
    {"symbol": "NDX", "name": "Nasdaq 100", "kind": "index"},
    {"symbol": "DJI", "name": "Dow Jones", "kind": "index",
     "candidates": ["DJI", "INDU", "DJIA", "$DJI", ".DJI", "DJX.XO"],
     "min_level": 1000},
    {"symbol": "RUT", "name": "Russell 2000", "kind": "index"},
    {"symbol": "VIX", "name": "Volatility", "kind": "volatility"},
]


def _candidates(ix: dict) -> list[str]:
    """Streamer symbols to try for an index, in preference order."""
    return ix.get("candidates") or [ix["symbol"]]


def _resolve(ix: dict, quotes: dict[str, dict]) -> tuple[str, dict]:
    """Pick the first candidate that returned a usable level (>= ``min_level``).

    Returns the chosen streamer symbol and its quote dict. Falls back to the
    primary symbol's (possibly empty) quote when nothing qualifies."""
    floor = ix.get("min_level", 0)
    for cand in _candidates(ix):
        q = quotes.get(cand) or {}
        if q.get("last") is not None and q["last"] >= floor:
            return cand, q
    return ix["symbol"], quotes.get(ix["symbol"], {})

# Fields surfaced per index when no live data is available (keeps the JSON shape
# stable so the UI renders placeholders rather than crashing).
_NULL_QUOTE = {"last": None, "change": None, "change_pct": None,
               "week_change_pct": None, "prev_close": None}


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

    if not session_manager.is_authenticated():
        base["note"] = "Live market data requires authentication."
        base["indices"] = [{**ix, **_NULL_QUOTE} for ix in INDICES]
        return base

    # Fetch quotes and weekly performance **concurrently** (they hit independent
    # DXLink streams) so first paint is bounded by the slower of the two, not their
    # sum. Quotes use a short timeout: when the market is open an index Trade lands
    # in well under a second, and the only reason to wait longer is the dead Dow
    # candidate symbols that never report — which we don't want to wait on.
    fetch = [s for ix in INDICES for s in _candidates(ix)]
    quotes, week_changes = await asyncio.gather(
        get_client().get_quotes(fetch, timeout=2.5),
        _weekly_changes(fetch),
        return_exceptions=True,
    )
    if isinstance(quotes, Exception):  # overview must never 500
        logger.warning("Overview quote fetch failed: %s", quotes)
        base["note"] = "Market data feed temporarily unavailable."
        base["indices"] = [{**ix, **_NULL_QUOTE} for ix in INDICES]
        return base
    if isinstance(week_changes, Exception):  # weekly is best-effort, never fatal
        logger.warning("Weekly change fetch failed: %s", week_changes)
        week_changes = {}

    # Resolve each index to the candidate that actually streamed a usable level.
    resolved = [(ix, *_resolve(ix, quotes)) for ix in INDICES]

    indices = []
    for ix, sym, q in resolved:
        indices.append({
            **ix,
            "symbol": sym,  # the symbol that actually resolved (may differ from default)
            "last": q.get("last"),
            "change": q.get("change"),
            "change_pct": q.get("change_pct"),
            "prev_close": q.get("prev_close"),
            "day_volume": q.get("day_volume"),
            "week_change_pct": week_changes.get(sym),
        })
    base["indices"] = indices
    # "Available" only if at least one index actually returned a level — a board
    # of all-nulls (e.g. feed outage) should still read as degraded.
    base["data_available"] = any(ix["last"] is not None for ix in indices)
    if not base["data_available"]:
        base["note"] = "Index feed returned no data (markets may be closed)."
    return base


# Weekly % change moves only on the daily close, so it's cached to keep it off the
# hot path — the dashboard polls every 15s and we don't want to re-pull two weeks
# of candles (and re-probe the dead Dow candidates) each time.
_WEEKLY_TTL = 600.0  # seconds
_weekly_cache: dict[str, tuple[float | None, float]] = {}
# Each candle fetch opens its own (expensive) DXLink connection, so cap how many
# run at once — opening one per symbol in parallel risks the per-session connection
# limit. Weekly is off the hot path (cached), so a small cap costs nothing visible.
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
