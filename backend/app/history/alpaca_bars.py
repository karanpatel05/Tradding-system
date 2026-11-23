"""Alpaca market-data client for 1-minute stock bars (paged, rate limited).

Calls the data host directly with the shared key headers; the trading-API `request()`
helper is paper-locked and not used here. Keys only ever travel as request headers."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from app.broker.alpaca_client import headers

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
_TIMEOUT = 30.0


class AlpacaDataError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Alpaca data HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.recent_sip_denied = status == 403 and "recent sip" in message.lower()


class RateLimiter:
    """Spaces acquire() calls at least 60/per_minute seconds apart."""

    def __init__(self, per_minute: int = 180):
        self._gap = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(time.monotonic(), self._next) + self._gap


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_bar(raw: dict) -> dict | None:
    try:
        t = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
        return {"time": int(t.timestamp() * 1000), "open": float(raw["o"]),
                "high": float(raw["h"]), "low": float(raw["l"]),
                "close": float(raw["c"]), "volume": float(raw.get("v") or 0)}
    except (KeyError, TypeError, ValueError):
        return None


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
    except ValueError:
        pass
    return (resp.text or "").strip()[:200] or resp.reason_phrase


async def fetch_minutes(symbol: str, start: datetime, end: datetime, *,
                        feed: str = "sip", client: httpx.AsyncClient | None = None,
                        limiter: RateLimiter | None = None) -> list[dict]:
    """1-minute bars for `symbol` in [start, end), oldest first, following page tokens."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    limiter = limiter or RateLimiter()
    params = {"symbols": symbol, "timeframe": "1Min", "start": _rfc3339(start),
              "end": _rfc3339(end), "feed": feed, "adjustment": "split",
              "limit": 10000, "sort": "asc"}
    out: list[dict] = []
    try:
        while True:
            await limiter.acquire()
            resp = await http.get(BARS_URL, params=params, headers=headers())
            if resp.status_code != 200:
                raise AlpacaDataError(resp.status_code, _error_message(resp))
            body = resp.json()
            for raw in (body.get("bars") or {}).get(symbol) or []:
                bar = _to_bar(raw)
                if bar is not None:
                    out.append(bar)
            token = body.get("next_page_token")
            if not token:
                return out
            params = {**params, "page_token": token}
    finally:
        if own:
            await http.aclose()
