"""Polygon options provider: pure helpers + async client."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import httpx

from app.options.base import (Bar, OptionContract, OptionsDataProvider,
                              OptionsProviderError, PolygonNotAuthorized)

_TF = {"1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
       "1h": (1, "hour"), "1d": (1, "day")}


def _tf(timeframe: str) -> tuple[int, str]:
    try:
        return _TF[timeframe]
    except KeyError:
        raise ValueError(f"Unsupported timeframe: {timeframe}")


def _fmt(d: date | datetime) -> str:
    return (d.date() if isinstance(d, datetime) else d).isoformat()


def _agg_path(ticker: str, timeframe: str, start, end) -> str:
    mult, span = _tf(timeframe)
    return f"/v2/aggs/ticker/{ticker}/range/{mult}/{span}/{_fmt(start)}/{_fmt(end)}"


def _normalize_bar(r: dict, ticker: str) -> Bar:
    return {"time": int(r["t"]), "open": float(r["o"]), "high": float(r["h"]),
            "low": float(r["l"]), "close": float(r["c"]),
            "volume": float(r.get("v", 0.0)), "symbol": ticker}


def _normalize_contract(r: dict) -> OptionContract:
    return OptionContract(
        ticker=r["ticker"], underlying=r["underlying_ticker"],
        expiration=date.fromisoformat(r["expiration_date"]),
        strike=float(r["strike_price"]),
        right="C" if r["contract_type"] == "call" else "P",
        shares_per_contract=int(r.get("shares_per_contract", 100)))


def _check_status(status_code: int, body: dict | None) -> None:
    body = body or {}
    if status_code == 403 and body.get("status") == "NOT_AUTHORIZED":
        raise PolygonNotAuthorized(body.get("message", "Polygon plan not authorized"))
    if status_code >= 400:
        raise OptionsProviderError(f"Polygon HTTP {status_code}: {body.get('message', body)}")


class PolygonOptionsProvider(OptionsDataProvider):
    name = "polygon-options"

    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io",
                 *, max_retries: int = 5, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout

    async def _get(self, url: str, params: dict | None = None) -> dict:
        full = url if url.startswith("http") else f"{self.base_url}{url}"
        p = dict(params or {})
        p["apiKey"] = self.api_key
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            backoff = 1.0
            for _ in range(self.max_retries):
                r = await client.get(full, params=p)
                if r.status_code == 429:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                try:
                    body = r.json()
                except Exception:  # noqa: BLE001
                    body = {}
                _check_status(r.status_code, body)
                return body
            raise OptionsProviderError("Polygon rate limit: retries exhausted")

    async def list_contracts(self, underlying, *, expired=False, expiration_date=None,
                             contract_type=None, limit=1000) -> list[OptionContract]:
        params = {"underlying_ticker": underlying.upper(), "expired": str(expired).lower(),
                  "limit": min(limit, 1000)}
        if expiration_date:
            params["expiration_date"] = expiration_date
        if contract_type:
            params["contract_type"] = contract_type
        out: list[OptionContract] = []
        body = await self._get("/v3/reference/options/contracts", params)
        while True:
            out.extend(_normalize_contract(r) for r in body.get("results", []))
            if len(out) >= limit or not body.get("next_url"):
                break
            body = await self._get(body["next_url"])
        return out[:limit]

    async def get_aggregates(self, option_ticker, timeframe, start, end) -> list[Bar]:
        path = _agg_path(option_ticker, timeframe, start, end)
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}
        out: list[Bar] = []
        body = await self._get(path, params)
        while True:
            out.extend(_normalize_bar(r, option_ticker) for r in body.get("results", []))
            if not body.get("next_url"):
                break
            body = await self._get(body["next_url"])
        return out
