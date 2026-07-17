"""Data-provider abstraction.

Market data comes from Tastytrade:
  • HISTORICAL forward-refresh / scanner → ``providers/tastytrade_historical.py``
    (bulk backtests read cached ``data_cache/*.json`` directly, not a live provider)
  • LIVE quotes / stream / accounts / positions / orders → ``app/tastytrade/`` + ``engine/execution.py``

``HistoricalDataProvider`` is the seam: swapping historical sources is a
config/factory change, not a rewrite of every caller. Bars are normalized to one
shape so everything downstream (indicators, scanner, backtest, cache) is provider-
agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

# Canonical timeframe tokens. These MUST match the interval tokens used as cache
# keys and as the strategy's ``candle_interval`` so there are no silent misses.
TIMEFRAMES = ("1m", "5m", "15m", "1d")


class ProviderError(RuntimeError):
    """Base class for provider failures."""


# A normalized bar: {"time": int(ms, UTC), "open","high","low","close","volume": float, "symbol": str}
Bar = dict


class HistoricalDataProvider(ABC):
    """Read-only historical OHLCV bars. Returns oldest→newest, normalized."""

    name: str = "historical"

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | date | None = None,
        end: datetime | date | None = None,
    ) -> list[Bar]:
        """Historical bars for ``symbol`` at ``timeframe`` (one of ``TIMEFRAMES``)
        from ``start`` to ``end`` (inclusive; ``end`` defaults to now)."""
        raise NotImplementedError
