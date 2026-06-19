"""IBKR historical bars (forward refresh / scanner only — NOT bulk backfill;
IBKR intraday pacing is strict and depth is limited). Normalized to the canonical
bar shape. Bulk backtests continue to read cached data_cache/*.json."""
from __future__ import annotations

from datetime import datetime

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.providers.base import Bar, HistoricalDataProvider

_TF_TO_IB = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins", "1d": "1 day"}


def normalize_bar(b, symbol: str) -> Bar:
    # formatDate=2 -> b.date is epoch seconds (int) for intraday; for daily it's a date.
    if isinstance(b.date, (int, float)):
        ms = int(b.date) * 1000
    else:
        d = b.date if isinstance(b.date, datetime) else datetime(b.date.year, b.date.month, b.date.day)
        ms = int(d.timestamp() * 1000)
    return {"time": ms, "open": float(b.open), "high": float(b.high), "low": float(b.low),
            "close": float(b.close), "volume": float(b.volume), "symbol": symbol}


class IBKRHistoricalProvider(HistoricalDataProvider):
    name = "ibkr"

    async def get_bars(self, symbol, timeframe, start=None, end=None) -> list[Bar]:
        ib = await get_gateway().get_ib()
        bar_size = _TF_TO_IB[timeframe]
        duration = self._duration(start, timeframe)
        contract = stock(symbol)
        await ib.qualifyContractsAsync(contract)
        bars = await ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
            whatToShow="TRADES", useRTH=True, formatDate=2)
        return [normalize_bar(b, symbol.upper()) for b in bars]

    @staticmethod
    def _duration(start, timeframe) -> str:
        if start is None:
            return "1 Y" if timeframe == "1d" else "5 D"
        start_date = start.date() if isinstance(start, datetime) else start
        days = max(1, (datetime.now().date() - start_date).days)
        return f"{min(days, 365)} D" if timeframe == "1d" else f"{min(days, 30)} D"
