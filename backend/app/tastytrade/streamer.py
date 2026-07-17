"""One shared DXLink websocket for the whole app. Quote/Trade events are pumped into
in-memory caches (subscribe once per symbol, read many); Candle events serve history.
All failures degrade to None/[] - the facade decides about fallback. DXFeed keeps
serving last-known quotes after hours, which is the point of this migration."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle, Quote, Trade

from app.tastytrade.session import get_tt_session

logger = logging.getLogger("trading.tt_streamer")

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1d": "1d"}


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


class TTStreamer:
    def __init__(self) -> None:
        self._dx = None
        self._lock = asyncio.Lock()
        self._candle_lock = asyncio.Lock()
        self._pumps: list[asyncio.Task] = []
        self._quotes: dict[str, dict] = {}
        self._trades: dict[str, float] = {}
        self._candles: dict[str, dict[int, dict]] = {}
        self._subscribed: dict[str, set[str]] = {"Quote": set(), "Trade": set()}

    # --- connection -----------------------------------------------------
    async def _connect(self) -> bool:
        session = await get_tt_session()
        if session is None:
            return False
        try:
            self._dx = await DXLinkStreamer(session).__aenter__()
        except Exception:  # noqa: BLE001 - degrade, facade falls back
            logger.warning("DXLink connect failed", exc_info=True)
            self._dx = None
            return False
        self._start_pumps()
        return True

    async def _ensure(self) -> bool:
        if self._dx is not None:
            return True
        async with self._lock:
            if self._dx is not None:
                return True
            return await self._connect()

    def _start_pumps(self) -> None:
        self._pumps = [
            asyncio.create_task(self._pump(Quote)),
            asyncio.create_task(self._pump(Trade)),
            asyncio.create_task(self._pump(Candle)),
        ]

    async def _pump(self, cls) -> None:
        try:
            async for ev in self._dx.listen(cls):
                if cls is Quote:
                    self._quotes[ev.event_symbol] = {
                        "bid": _num(ev.bid_price), "ask": _num(ev.ask_price)}
                elif cls is Trade:
                    p = _num(ev.price)
                    if p is not None:
                        self._trades[ev.event_symbol] = p
                else:  # Candle: event_symbol like "QQQ{=5m}"
                    bucket = self._candles.setdefault(ev.event_symbol, {})
                    t = int(ev.time)
                    bucket[t] = {"time": t, "open": _num(ev.open), "high": _num(ev.high),
                                 "low": _num(ev.low), "close": _num(ev.close),
                                 "volume": _num(ev.volume) or 0.0}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - websocket died; drop so next call reconnects
            logger.warning("DXLink pump (%s) died; will reconnect on demand",
                           cls.__name__, exc_info=True)
            await self.teardown()

    async def teardown(self) -> None:
        for t in self._pumps:
            t.cancel()
        self._pumps = []
        dx, self._dx = self._dx, None
        self._subscribed = {"Quote": set(), "Trade": set()}
        if dx is not None:
            try:
                await dx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # --- reads ------------------------------------------------------------
    async def _wait_for(self, cache: dict, symbol: str, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = cache.get(symbol)
            if v is not None:
                return v
            await asyncio.sleep(0.1)
        return cache.get(symbol)

    async def quote(self, symbol: str, timeout: float = 3.0) -> dict | None:
        if not await self._ensure():
            return None
        if symbol not in self._subscribed["Quote"]:
            await self._dx.subscribe(Quote, [symbol])
            self._subscribed["Quote"].add(symbol)
        q = await self._wait_for(self._quotes, symbol, timeout)
        return q if q and (q["bid"] is not None or q["ask"] is not None) else None

    async def trade_price(self, symbol: str, timeout: float = 3.0) -> float | None:
        if not await self._ensure():
            return None
        if symbol not in self._subscribed["Trade"]:
            await self._dx.subscribe(Trade, [symbol])
            self._subscribed["Trade"].add(symbol)
        return await self._wait_for(self._trades, symbol, timeout)

    async def candles(self, symbol: str, interval: str,
                      start: datetime, end: datetime | None = None,
                      idle_seconds: float = 2.0) -> list[dict]:
        """Collect Candle history: subscribe from `start`, wait until no new events
        arrive for `idle_seconds`, unsubscribe, return bars in [start, end] oldest-first."""
        dx_interval = _INTERVALS.get(interval)
        if dx_interval is None or not await self._ensure():
            return []
        # dxfeed NORMALIZES the echoed candle symbol: subscribe "QQQ{=1d}" and events
        # arrive as "QQQ{=d,tho=true}" (canonical interval + trading-hours flag). Match
        # buckets by PREFIX, never by the exact subscription string.
        prefix = f"{symbol}{{="

        def _bucket_count() -> int:
            return sum(len(b) for k, b in self._candles.items() if k.startswith(prefix))

        async with self._candle_lock:            # one candle collection at a time
            for k in [k for k in self._candles if k.startswith(prefix)]:
                del self._candles[k]
            await self._dx.subscribe_candle([symbol], dx_interval, start_time=start)
            seen = -1
            while True:
                await asyncio.sleep(idle_seconds)
                n = _bucket_count()
                if n == seen:
                    break
                seen = n
            try:
                await self._dx.unsubscribe_candle([symbol], dx_interval)
            except Exception:  # noqa: BLE001
                pass
            merged: dict[int, dict] = {}
            for k in [k for k in self._candles if k.startswith(prefix)]:
                merged.update(self._candles.pop(k))
            bars = sorted(merged.values(), key=lambda b: b["time"])
        lo = int(start.timestamp() * 1000)
        hi = int((end or datetime.now(timezone.utc)).timestamp() * 1000)
        # close must be a POSITIVE number: a still-forming candle can arrive with 0.0.
        return [{**b, "symbol": symbol} for b in bars
                if lo <= b["time"] <= hi and (b["close"] or 0) > 0]


_streamer: TTStreamer | None = None


def get_streamer() -> TTStreamer:
    global _streamer
    if _streamer is None:
        _streamer = TTStreamer()
    return _streamer
