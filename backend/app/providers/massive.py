"""Massive (Polygon) historical data provider.

Massive is a rebrand of Polygon.io and exposes the same REST aggregates API:
``/v2/aggs/ticker/{T}/range/{mult}/{timespan}/{from}/{to}``. Polygon's ``t`` field
is the bar's window-start in **Unix milliseconds (UTC)** — exactly the ``time``
shape the rest of the app already uses, so normalization is a 1:1 field map.

Handles: split-``adjusted`` data (explicit), ascending sort, pagination via
``next_url``, a bounded concurrency semaphore, and 429 rate-limit backoff.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from app.providers.base import HistoricalDataProvider, MassiveNotConfigured

logger = logging.getLogger("trading.providers.massive")

# Canonical timeframe → (multiplier, Polygon timespan).
_TF: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "1d": (1, "day"),
}


def _fmt(d: datetime | date | None, default_days_back: int) -> str:
    if d is None:
        d = datetime.now(timezone.utc) - timedelta(days=default_days_back)
    if isinstance(d, datetime):
        d = d.astimezone(timezone.utc).date() if d.tzinfo else d.date()
    return d.isoformat()  # Polygon accepts YYYY-MM-DD


class MassiveHistoricalProvider(HistoricalDataProvider):
    name = "massive"

    def __init__(self, api_key: str | None, base_url: str = "https://api.polygon.io",
                 *, max_concurrency: int = 5, retry_base: float = 1.0, max_retries: int = 4,
                 min_request_interval: float = 0.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._sem = asyncio.Semaphore(max_concurrency)
        self._retry_base = retry_base
        self._max_retries = max_retries
        # Proactive pacing between HTTP requests (seconds). 0 = off (default), so the
        # live app is unaffected; bulk jobs set this to stay under a tier's req/min
        # cap (e.g. 13s ≈ the Polygon free tier's 5/min) instead of relying on 429
        # backoff, which can exhaust its retries on a long paginated pull.
        self._min_interval = min_request_interval
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0
        self._transport = transport  # injectable for tests

    async def get_bars(self, symbol, timeframe, start=None, end=None) -> list[dict]:
        if not self._api_key:
            raise MassiveNotConfigured("MASSIVE_API_KEY not set — historical data unavailable.")
        if timeframe not in _TF:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {tuple(_TF)}")

        mult, span = _TF[timeframe]
        frm = _fmt(start, default_days_back=730)
        to = _fmt(end, default_days_back=0)
        url = (f"{self._base}/v2/aggs/ticker/{symbol}/range/{mult}/{span}/{frm}/{to}")
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self._api_key}

        logger.info("[Massive] Historical data request %s %s %s→%s", symbol, timeframe, frm, to)
        bars: list[dict] = []
        async with self._sem:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
                next_url: str | None = url
                first = True
                while next_url:
                    data = await self._get(client, next_url, params if first else {"apiKey": self._api_key})
                    for a in data.get("results") or []:
                        bars.append({
                            "time": int(a["t"]),  # Unix ms, UTC — used as-is everywhere
                            "open": a.get("o"),
                            "high": a.get("h"),
                            "low": a.get("l"),
                            "close": a.get("c"),
                            "volume": a.get("v"),
                            "symbol": symbol,
                        })
                    next_url = data.get("next_url")
                    first = False

        bars.sort(key=lambda b: b["time"])
        logger.info("[Massive] Historical data received %s %s bars=%d", symbol, timeframe, len(bars))
        return bars

    async def _pace(self) -> None:
        """Enforce ``min_request_interval`` between request starts (no-op when 0)."""
        if self._min_interval <= 0:
            return
        async with self._pace_lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = asyncio.get_event_loop().time()

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict,
                   max_retries: int | None = None) -> dict:
        """GET with proactive pacing + graceful 429 backoff. Raises on other HTTP errors."""
        max_retries = self._max_retries if max_retries is None else max_retries
        for attempt in range(max_retries + 1):
            await self._pace()
            resp = await client.get(url, params=params)
            if resp.status_code == 429 and attempt < max_retries:
                wait = float(resp.headers.get("Retry-After") or self._retry_base * (2 ** attempt))
                logger.warning("[Massive] rate-limited (429); retrying in %.1fs", wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # exhausted retries on 429
        return {}
