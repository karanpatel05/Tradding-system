"""Persistent server-side market-data stream.

Holds a **single long-lived DXLink connection** multiplexing ``Trade`` + ``Summary``
for every index candidate symbol. The DXLink handshake is expensive (an HTTP token
fetch plus a multi-roundtrip websocket auth), so we pay it once and keep the socket
open rather than re-opening a streamer on every poll. Incoming events continuously
update an in-memory cache; the WebSocket push loop reads that cache synchronously
(a plain dict access — it never awaits the feed) and forwards it to the browser, so
prices stream live.

Robust by construction:
* A symbol that never reports (e.g. the dead Dow candidate symbols) simply has no
  cache entry — there is no per-poll timeout to wait out.
* The task reconnects with exponential backoff on a dropped connection.
* It self-stops when the session is cleared (logout), and is restarted lazily the
  next time an authenticated WebSocket client connects.

The REST ``/market/overview`` endpoint remains as a fallback so a silent stream
failure still renders a board.
"""

from __future__ import annotations

import asyncio
import logging

from app.auth.manager import session_manager
from app.services.market import (
    INDICES,
    _candidates,
    _resolve,
    cached_week_change,
    get_market_status,
)
from app.tastytrade.client import _f

logger = logging.getLogger("trading.market_stream")

# Empty per-symbol record; mirrors the get_quotes shape.
def _blank(sym: str) -> dict:
    return {"symbol": sym, "last": None, "change": None, "change_pct": None,
            "prev_close": None, "day_volume": None}


class MarketStream:
    """Owns the persistent quote stream + the latest-quote cache (singleton)."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._latest: dict[str, dict] = {}
        self._connected = False

    # --- lifecycle -------------------------------------------------------
    def ensure_started(self) -> None:
        """Idempotently launch the background stream task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the stream and drop the cache (logout)."""
        task, self._task = self._task, None
        self._connected = False
        self._latest.clear()
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
        """Build the overview dict from the live cache — synchronous and instant.

        Identical shape to ``market.get_market_overview`` so the frontend renders it
        the same way, plus a ``streaming`` flag."""
        resolved = [(ix, *_resolve(ix, self._latest)) for ix in INDICES]
        indices = []
        for ix, sym, q in resolved:
            indices.append({
                **ix,
                "symbol": sym,
                "last": q.get("last"),
                "change": q.get("change"),
                "change_pct": q.get("change_pct"),
                "prev_close": q.get("prev_close"),
                "day_volume": q.get("day_volume"),
                "week_change_pct": cached_week_change(sym),
            })
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
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Summary, Trade

        symbols = [s for ix in INDICES for s in _candidates(ix)]
        backoff = 1.0
        while True:
            if not session_manager.is_authenticated():
                self._connected = False
                return  # nothing to stream; a future WS connect restarts us
            session = session_manager.tasty_session
            try:
                async with DXLinkStreamer(session) as streamer:
                    await streamer.subscribe(Trade, symbols)
                    await streamer.subscribe(Summary, symbols)
                    self._connected = True
                    backoff = 1.0
                    logger.info("Market stream connected (%d symbols)", len(symbols))
                    warm = asyncio.create_task(self._warm_weekly())
                    try:
                        await asyncio.gather(
                            self._pump(streamer, Trade),
                            self._pump(streamer, Summary),
                        )
                    finally:
                        warm.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any feed error
                logger.warning("Market stream error (%s); reconnecting in %.0fs", exc, backoff)
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _pump(self, streamer, event_class) -> None:
        """Consume one event type forever, folding it into the cache."""
        from tastytrade.dxfeed import Trade

        async for ev in streamer.listen(event_class):
            sym = getattr(ev, "event_symbol", None)
            if not sym:
                continue
            q = self._latest.setdefault(sym, _blank(sym))
            if event_class is Trade:
                price = _f(ev.price)
                if price is not None:
                    q["last"] = price
                q["day_volume"] = _f(ev.day_volume)
                change = _f(ev.change)
                if change is not None:
                    q["change"] = change
            else:  # Summary — carries the prior close, the basis for index day-change
                prev = _f(ev.prev_day_close_price)
                if prev is not None:
                    q["prev_close"] = prev
            # Derive change/pct from prev_close when the feed didn't supply one
            # (the usual case for cash indices).
            if q["change"] is None and q["last"] is not None and q["prev_close"]:
                q["change"] = round(q["last"] - q["prev_close"], 4)
            if q["change"] is not None and q["prev_close"]:
                q["change_pct"] = round(q["change"] / q["prev_close"], 4)

    async def _warm_weekly(self) -> None:
        """Keep the weekly-% cache warm for the symbols that actually resolved.

        Runs alongside the live stream; ``_weekly_changes`` is itself cached +
        concurrency-capped, so this is cheap and best-effort."""
        from app.services.market import _WEEKLY_TTL, _weekly_changes

        while True:
            await asyncio.sleep(2)  # let first prices land so resolution is meaningful
            resolved = [_resolve(ix, self._latest)[0] for ix in INDICES]
            if any(self._latest.get(s, {}).get("last") is not None for s in resolved):
                try:
                    await _weekly_changes(resolved)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("weekly warm failed: %s", exc)
            await asyncio.sleep(_WEEKLY_TTL)


# Module-level singleton.
market_stream = MarketStream()
