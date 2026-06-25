"""Real price source for the Triple Calendar backtest, over Polygon. Option closes
come from the existing options provider's daily aggregates; the underlying's daily
closes come from Polygon stock aggregates (same key). Per-(contract) daily series are
cached in-memory per run to stay pacing-friendly."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import httpx

from app.config import get_settings
from app.options import get_options_provider
from app.options.contracts import occ_ticker


def _ms_to_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


class PolygonPriceSource:
    def __init__(self, underlying: str, start: date, end: date):
        self._underlying = underlying.upper()
        self._prov = get_options_provider()
        self._settings = get_settings()
        self._start, self._end = start, end
        self._under: dict[date, float] | None = None
        self._opt: dict[str, dict[date, float]] = {}
        self._chain: dict[tuple, list[float]] = {}

    # ----- underlying (Polygon stock aggregates) -----
    def _load_underlying(self) -> dict[date, float]:
        if self._under is None:
            url = (f"{self._settings.polygon_base_url}/v2/aggs/ticker/{self._underlying}"
                   f"/range/1/day/{self._start.isoformat()}/{self._end.isoformat()}")
            r = httpx.get(url, params={"adjusted": "true", "sort": "asc", "limit": 50000,
                                       "apiKey": self._settings.polygon_api_key}, timeout=30)
            r.raise_for_status()
            self._under = {_ms_to_date(b["t"]): float(b["c"]) for b in r.json().get("results", [])}
        return self._under

    def trading_days(self, start: date, end: date) -> list[date]:
        return sorted(d for d in self._load_underlying() if start <= d <= end)

    def underlying_close(self, symbol: str, day: date) -> float | None:
        return self._load_underlying().get(day)

    # ----- options (existing Polygon options provider) -----
    def _load_option(self, ticker: str) -> dict[date, float]:
        if ticker not in self._opt:
            try:
                bars = asyncio.run(self._prov.get_aggregates(ticker, "1d", self._start, self._end))
                self._opt[ticker] = {_ms_to_date(b["time"]): b["close"] for b in bars}
            except Exception:  # noqa: BLE001 — missing/unauthorized contract -> empty series
                self._opt[ticker] = {}
        return self._opt[ticker]

    def option_close(self, underlying: str, expiry: date, strike: float, right: str,
                     day: date) -> float | None:
        ticker = occ_ticker(underlying, expiry, right, strike)
        return self._load_option(ticker).get(day)

    def chain_strikes(self, underlying: str, expiry: date) -> list[float]:
        key = (underlying.upper(), expiry)
        if key not in self._chain:
            try:
                cons = asyncio.run(self._prov.list_contracts(
                    underlying, expiration_date=expiry.isoformat(), contract_type="call", limit=1000))
                self._chain[key] = sorted({c.strike for c in cons})
            except Exception:  # noqa: BLE001
                self._chain[key] = []
        return self._chain[key]
