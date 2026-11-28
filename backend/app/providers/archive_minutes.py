"""Read complete extended-hours DXLink minute snapshots for the archive."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle

from app.tastytrade.session import get_tt_session

_MINUTE_MS = 60_000
_CONNECT_SECONDS = 20
_SNAPSHOT_SECONDS = 45


class ArchiveMinuteSource:
    """One archive pass owns this socket, independently of chart and strategy subscriptions."""

    def __init__(self, dx: DXLinkStreamer):
        self._dx = dx
        self._lock = asyncio.Lock()  # The SDK has one receive queue for all Candle symbols.
        self._failed: set[str] = set()

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        """Return the provider's closed candles in [start, end), or raise on incomplete history."""
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise ValueError("Archive range must use timezone-aware, increasing timestamps")
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        if hi > int(datetime.now(end.tzinfo).timestamp() * 1000):
            raise ValueError("Archive range must end in the past")
        async with self._lock:
            if symbol in self._failed:
                raise RuntimeError(f"Retry {symbol} on a fresh archive connection")
            # A timed-out snapshot may still have events buffered on this socket.
            # Reusing that symbol is safe only after receiving its completion marker.
            self._failed.add(symbol)
            try:
                async with asyncio.timeout(_SNAPSHOT_SECONDS):
                    await self._dx.subscribe_candle([symbol], "1m", start_time=start,
                                                    extended_trading_hours=True)
                    bars = await self._read_snapshot(symbol, lo, hi)
            finally:
                async with asyncio.timeout(5):
                    await self._dx.unsubscribe_candle(symbol, "1m", extended_trading_hours=True)
            self._failed.remove(symbol)
            return bars

    async def _read_snapshot(self, symbol: str, lo: int, hi: int) -> list[dict]:
        # dxFeed canonicalizes '=1m' to '=m' and omits the default hours flag.
        keys = {f"{symbol}{{=m}}", f"{symbol}{{=1m}}"}
        rows: dict[int, dict] = {}
        started = ended = False
        async for event in self._dx.listen(Candle):
            if event.event_symbol not in keys:
                continue
            if event.snapshot_begin:
                rows.clear()  # A new snapshot supersedes a partial one.
                started, ended = True, False
            if not started:
                continue
            if event.snapshot_snip:
                raise RuntimeError(f"Truncated candle history for {symbol}")
            t = event.time
            if lo <= t and t + _MINUTE_MS <= hi:
                if event.remove:
                    rows.pop(t, None)
                else:
                    rows[t] = {"time": t, "open": float(event.open), "high": float(event.high),
                               "low": float(event.low), "close": float(event.close),
                               "volume": float(event.volume)}
            # DXFeed delivers history newest first. A boundary record may be a
            # removal with zero OHLC when no trade occurred at the requested start.
            if event.snapshot_end or t <= lo:
                if t > lo:
                    raise RuntimeError(f"Candle history for {symbol} ended before covering the request")
                ended = True
            if ended and not event.pending:
                return [rows[t] for t in sorted(rows)]
        raise RuntimeError(f"Archive feed disconnected before completing {symbol}")


@asynccontextmanager
async def open_minute_source():
    session = await get_tt_session()
    if session is None:
        raise RuntimeError("Tastytrade is not configured for the minute archive")
    async with AsyncExitStack() as stack:
        async with asyncio.timeout(_CONNECT_SECONDS):
            dx = await stack.enter_async_context(DXLinkStreamer(session))
        yield ArchiveMinuteSource(dx)
