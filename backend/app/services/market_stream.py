"""Persistent server-side market-data stream (IBKR / ib_async).

Subscribes once to live tickers for the overview ETF proxies and keeps them open.
ib_async updates each ``Ticker`` object in place as ticks arrive, so the WebSocket
push loop reads the latest values synchronously (no awaiting the feed) via
``overview()``. The task self-stops when disconnected and is restarted lazily the
next time a WebSocket client connects.

The REST ``/market/overview`` endpoint remains as a fallback so a silent stream
failure still renders a board.
"""

from __future__ import annotations

import asyncio
import logging

from app.auth.manager import session_manager
from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.ibkr.market import normalize_ticker
from app.services.market import INDICES, index_row, get_market_status

logger = logging.getLogger("trading.market_stream")

_SYMBOLS = [ix["symbol"] for ix in INDICES]


class MarketStream:
    """Owns the persistent quote subscription + latest tickers (singleton)."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._tickers: dict[str, object] = {}
        self._connected = False

    # --- lifecycle -------------------------------------------------------
    def ensure_started(self) -> None:
        """Idempotently launch the background stream task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the stream and drop the cache."""
        task, self._task = self._task, None
        self._connected = False
        self._tickers.clear()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def connected(self) -> bool:
        return self._connected

    # --- snapshot --------------------------------------------------------
    def overview(self) -> dict:
        """Build the overview dict from the live tickers — synchronous and instant.
        Same shape as ``market.get_market_overview`` plus a ``streaming`` flag."""
        indices = []
        for ix in INDICES:
            t = self._tickers.get(ix["symbol"])
            quote = normalize_ticker(t) if t is not None else None
            indices.append(index_row(ix, quote))
        available = any(i["last"] is not None for i in indices)
        note = None
        if not available:
            note = "Connecting to live market feed…" if self._connected else "Starting market feed…"
        return {
            "status": get_market_status(),
            "indices": indices,
            "data_available": available,
            "streaming": self._connected,
            "note": note,
        }

    # --- internals -------------------------------------------------------
    async def _run(self) -> None:
        backoff = 1.0
        while True:
            if not session_manager.is_authenticated():
                self._connected = False
                return  # nothing to stream; a future WS connect restarts us
            try:
                ib = await get_gateway().get_ib()
                self._tickers = {sym: ib.reqMktData(stock(sym), "", False, False)
                                 for sym in _SYMBOLS}
                self._connected = True
                backoff = 1.0
                logger.info("Market stream connected (%d symbols)", len(_SYMBOLS))
                warm = asyncio.create_task(self._warm_weekly())
                try:
                    # Tickers update in place; just stay alive while connected.
                    while get_gateway().is_connected():
                        await asyncio.sleep(1.0)
                finally:
                    warm.cancel()
                    for sym in _SYMBOLS:
                        try:
                            ib.cancelMktData(stock(sym))
                        except Exception:  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any feed error
                logger.warning("Market stream error (%s); reconnecting in %.0fs", exc, backoff)
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _warm_weekly(self) -> None:
        """Keep the weekly-% cache warm. ``_weekly_changes`` is itself cached +
        concurrency-capped, so this is cheap and best-effort."""
        from app.services.market import _WEEKLY_TTL, _weekly_changes

        while True:
            await asyncio.sleep(2)
            try:
                await _weekly_changes(_SYMBOLS)
            except Exception as exc:  # noqa: BLE001
                logger.debug("weekly warm failed: %s", exc)
            await asyncio.sleep(_WEEKLY_TTL)


# Module-level singleton.
market_stream = MarketStream()
