"""Provider factory. Historical/backtest data is routed here; live data stays on
the Tastytrade client. Swapping the historical source later is a change in this
one place."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.providers.base import HistoricalDataProvider, MassiveNotConfigured, ProviderError
from app.providers.massive import MassiveHistoricalProvider

__all__ = [
    "get_historical_provider",
    "HistoricalDataProvider",
    "MassiveNotConfigured",
    "ProviderError",
]


@lru_cache
def get_historical_provider() -> HistoricalDataProvider:
    """The configured historical-data provider (Massive)."""
    s = get_settings()
    return MassiveHistoricalProvider(s.massive_api_key, s.massive_base_url)
