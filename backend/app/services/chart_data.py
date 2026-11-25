"""Chart data for the Chart screen: candle history and watchlist quotes.

Candle history comes from the market-data facade (Tastytrade candles). A candle
collection subscribes, waits for the feed to go quiet and runs one at a time, so
results are cached briefly per (symbol, interval) to keep several chart panes and
the polling refresh from queueing behind each other. Everything degrades to an
empty result with a note; the frontend then shows its labelled sample data.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from app.marketdata.facade import bars as facade_bars, stock_quotes as facade_stock_quotes
from app.services.market import get_market_status

logger = logging.getLogger("trading.chart_data")

# Intervals the candle feed supports, with how much history each one loads.
BAR_LOOKBACK_DAYS = {"1m": 3, "5m": 10, "15m": 30, "1d": 400}
# Live strategies fetch their bars through the same one-at-a-time candle collector,
# so chart requests are cached long enough that several panes and tabs share one fetch.
BARS_TTL_SECONDS = 45.0
MAX_QUOTE_SYMBOLS = 20

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_bars_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_bars_locks: dict[tuple[str, str], asyncio.Lock] = {}


class ChartDataError(ValueError):
    """Invalid chart-data request (bad symbol or interval)."""


def normalize_symbol(raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise ChartDataError(f"Invalid symbol: {raw!r}")
    return symbol


def _clean_bar(bar: dict) -> dict | None:
    try:
        out = {
            "time": int(bar["time"]),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume") or 0.0),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if min(out["open"], out["high"], out["low"], out["close"]) <= 0:
        return None
    return out


async def get_bars(symbol: str, interval: str, now: float | None = None) -> dict:
    """Candle history for one symbol, oldest first. Never raises for feed problems."""
    symbol = normalize_symbol(symbol)
    if interval not in BAR_LOOKBACK_DAYS:
        raise ChartDataError(f"Unsupported interval: {interval!r}")
    key = (symbol, interval)
    clock = time.monotonic if now is None else (lambda: now)

    cached = _bars_cache.get(key)
    if cached and clock() - cached[0] < BARS_TTL_SECONDS:
        return cached[1]

    lock = _bars_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _bars_cache.get(key)  # another request may have filled it meanwhile
        if cached and clock() - cached[0] < BARS_TTL_SECONDS:
            return cached[1]
        start = datetime.now(timezone.utc) - timedelta(days=BAR_LOOKBACK_DAYS[interval])
        result = {"symbol": symbol, "interval": interval, "bars": [], "source": None, "note": None}
        try:
            raw = await facade_bars(symbol, interval, start)
        except Exception as exc:  # noqa: BLE001 - feed problems degrade, never 500
            logger.warning("Bar fetch failed for %s %s: %s", symbol, interval, exc)
            result["note"] = "Market data feed temporarily unavailable."
            return result  # failures are not cached, so the next poll retries
        bars = [b for b in (_clean_bar(x) for x in raw or []) if b]
        bars.sort(key=lambda b: b["time"])
        result["bars"] = bars
        if not bars:
            # Not cached: an empty answer usually means the feed is still (re)connecting,
            # and caching it would pin every chart pane to sample data for the TTL.
            result["note"] = "No candle data returned (feed not connected or market closed)."
            return result
        result["source"] = "tastytrade"
        _bars_cache[key] = (clock(), result)
        return result


async def get_quotes(raw_symbols: str) -> dict:
    """Last price and change from previous close for a short watchlist."""
    symbols: list[str] = []
    for part in (raw_symbols or "").split(","):
        if part.strip():
            s = normalize_symbol(part)
            if s not in symbols:
                symbols.append(s)
    if not symbols:
        raise ChartDataError("No symbols given")
    if len(symbols) > MAX_QUOTE_SYMBOLS:
        raise ChartDataError(f"At most {MAX_QUOTE_SYMBOLS} symbols")
    try:
        quotes = await facade_stock_quotes(symbols, timeout=2.5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Watchlist quote fetch failed: %s", exc)
        quotes = {}
    out: dict[str, dict] = {}
    for s in symbols:
        q = quotes.get(s) or {}
        price, close = q.get("price"), q.get("close")
        change = (price / close - 1) * 100 if price and close else None
        out[s] = {"price": price, "close": close, "bid": q.get("bid"), "ask": q.get("ask"),
                  "change_pct": change}
    available = any(v["price"] is not None for v in out.values())
    # The chart folds these prices into its newest candle only in the regular session; the
    # server's session knows NYSE holidays, so the browser never opens candles on a closed day.
    return {"quotes": out, "data_available": available, "session": get_market_status()["session"],
            "note": None if available else "No quote data (feed not connected or market closed)."}
