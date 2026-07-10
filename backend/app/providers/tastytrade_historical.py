"""Historical OHLCV bars via Tastytrade DXLink Candle events (already normalized by the
streamer to the provider Bar shape). Used by the factory when TT creds are configured."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone

from app.providers.base import TIMEFRAMES, HistoricalDataProvider, ProviderError

# Default lookback when no start is given, per timeframe (mirrors IBKR provider limits).
_DEFAULT_LOOKBACK = {"1m": timedelta(days=5), "5m": timedelta(days=30),
                     "15m": timedelta(days=60), "1d": timedelta(days=365 * 2)}


class TastytradeHistoricalProvider(HistoricalDataProvider):
    name = "tastytrade"

    async def _candles(self, symbol, interval, start, end=None, idle_seconds=2.0):
        from app.tastytrade.streamer import get_streamer
        return await get_streamer().candles(symbol, interval, start=start, end=end,
                                            idle_seconds=idle_seconds)

    async def get_bars(self, symbol, timeframe, start=None, end=None):
        if timeframe not in TIMEFRAMES:
            raise ProviderError(f"unsupported timeframe {timeframe!r}")
        now = datetime.now(timezone.utc)
        if start is None:
            start = now - _DEFAULT_LOOKBACK[timeframe]
        if isinstance(start, date) and not isinstance(start, datetime):
            start = datetime.combine(start, dtime.min, tzinfo=timezone.utc)
        if isinstance(end, date) and not isinstance(end, datetime):
            end = datetime.combine(end, dtime.max, tzinfo=timezone.utc)
        return await self._candles(symbol, timeframe, start=start, end=end)
