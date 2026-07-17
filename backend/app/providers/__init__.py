"""Provider factory. Historical/forward-refresh data is served exclusively by
Tastytrade (DXLink candles). Bulk backtests read cached files directly via
history_cache."""

from __future__ import annotations

from functools import lru_cache

from app.providers.base import HistoricalDataProvider, ProviderError

__all__ = ["get_historical_provider", "HistoricalDataProvider", "ProviderError"]


@lru_cache
def get_historical_provider() -> HistoricalDataProvider:
    from app.providers.tastytrade_historical import TastytradeHistoricalProvider
    return TastytradeHistoricalProvider()
