"""Options data-provider abstraction: normalized types + errors + the interface.
Bars reuse the canonical shape used elsewhere:
``{time(ms,UTC), open, high, low, close, volume, symbol}`` (symbol = option ticker)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime

Bar = dict


class OptionsProviderError(RuntimeError):
    """Base class for options-provider failures."""


class PolygonNotConfigured(OptionsProviderError):
    """No Polygon API key configured."""


class PolygonNotAuthorized(OptionsProviderError):
    """Polygon 403 — plan doesn't include the requested data/timeframe."""


@dataclass
class OptionContract:
    ticker: str
    underlying: str
    expiration: date
    strike: float
    right: str               # "C" | "P"
    shares_per_contract: int


class OptionsDataProvider(ABC):
    name: str = "options"

    @abstractmethod
    async def list_contracts(self, underlying: str, *, expired: bool = False,
                             expiration_date: str | None = None,
                             contract_type: str | None = None,
                             limit: int = 1000) -> list[OptionContract]:
        raise NotImplementedError

    @abstractmethod
    async def get_aggregates(self, option_ticker: str, timeframe: str,
                             start: date | datetime, end: date | datetime) -> list[Bar]:
        raise NotImplementedError
