"""Persistent server-side market-data stream (Tastytrade DXLink).

Subscribes once to the overview ETF proxies via the shared DXLink streamer
(``app.tastytrade.streamer``) and then relies on that streamer's own background
pumps to keep the quote/trade caches fresh — this module does no polling of its
own. ``overview()`` reads those caches synchronously (no awaiting the feed) so the
WebSocket push loop never blocks on it, and ``snapshot()`` wraps that into the same
envelope ``market.get_market_overview`` returns (``status``/``indices``/
``data_available``/``streaming``/``note``) for byte-compatible WS payloads.

The REST ``/market/overview`` endpoint remains as a fallback so a silent stream
failure still renders a board.
"""

from __future__ import annotations

import asyncio
import logging

from app.services.market import INDICES, index_row, get_market_status

logger = logging.getLogger("trading.market_stream")

_SYMBOLS = [ix["symbol"] for ix in INDICES]


def _streamer():
    """Indirection point so tests can fake the shared DXLink streamer."""
    from app.tastytrade.streamer import get_streamer
    return get_streamer()


class MarketStream:
    """Owns the persistent DXLink subscription for the overview symbols (singleton)."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    # --- lifecycle -------------------------------------------------------
    def ensure_started(self) -> None:
        """Idempotently launch the background subscribe task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._subscribe_all())

    def keep_alive(self) -> None:
        """Called every WS tick. After a DXLink drop the shared streamer clears its
        connection and subscriptions; re-reading the overview symbols reconnects and
        resubscribes them, so the board recovers without a page reload."""
        if not self.connected:
            self.ensure_started()

    async def stop(self) -> None:
        """Cancel any in-flight subscribe task. Does not tear down the shared
        streamer itself — other consumers (e.g. strategies) may still need it."""
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def connected(self) -> bool:
        """Best-effort: true once the shared streamer has an active DXLink socket."""
        try:
            return getattr(_streamer(), "_dx", None) is not None
        except Exception:  # noqa: BLE001
            return False

    # --- snapshot --------------------------------------------------------
    def overview(self) -> list[dict]:
        """Build the overview rows from the streamer's live caches — synchronous
        and instant. One row per ``INDICES`` entry, same shape as
        ``market.get_market_overview``'s ``indices`` list."""
        streamer = _streamer()
        rows = []
        for ix in INDICES:
            sym = ix["symbol"]
            q = streamer._quotes.get(sym)
            last = streamer._trades.get(sym)
            if last is None and q:
                bid, ask = q.get("bid"), q.get("ask")
                if bid is not None and ask is not None:
                    last = (bid + ask) / 2
            prev = getattr(streamer, "_prev_close", {}).get(sym)  # previous session close
            quote = {"last": last, "close": prev, "day_volume": None} if (q or last is not None) else None
            rows.append(index_row(ix, quote))
        return rows

    def snapshot(self) -> dict:
        """Envelope around ``overview()`` matching ``market.get_market_overview``'s
        shape plus a ``streaming`` flag — this is what the WS push loop sends."""
        indices = self.overview()
        available = any(i["last"] is not None for i in indices)
        note = None
        if not available:
            note = "Connecting to live market feed…" if self.connected else "Starting market feed…"
        return {
            "status": get_market_status(),
            "indices": indices,
            "data_available": available,
            "streaming": self.connected,
            "note": note,
        }

    # --- internals -------------------------------------------------------
    async def _subscribe_all(self) -> None:
        """Subscribe every overview symbol on the shared DXLink streamer once, then
        exit — the streamer's own pumps keep the caches fresh from here on."""
        streamer = _streamer()
        for sym in _SYMBOLS:
            try:
                await asyncio.gather(streamer.quote(sym), streamer.trade_price(sym))
            except Exception as exc:  # noqa: BLE001 - best-effort; overview() degrades
                logger.warning("Market stream subscribe failed for %s: %s", sym, exc)


# Module-level singleton.
market_stream = MarketStream()
