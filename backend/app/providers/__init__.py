"""Provider factory. Historical/forward-refresh data comes from IBKR; bulk
backtests read cached files directly via history_cache."""

from __future__ import annotations

from functools import lru_cache

from app.providers.base import HistoricalDataProvider, ProviderError
from app.providers.ibkr_historical import IBKRHistoricalProvider

__all__ = ["get_historical_provider", "HistoricalDataProvider", "ProviderError"]


@lru_cache
def get_historical_provider() -> HistoricalDataProvider:
    return IBKRHistoricalProvider()
