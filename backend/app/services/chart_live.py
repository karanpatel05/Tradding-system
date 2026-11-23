"""Live last-trade prices for the symbols a browser has open in chart panes.

Each ``/ws`` connection reports its chart symbols; every snapshot then carries the
latest cached Trade price for them, during the regular session only (the history
store holds regular-hours minutes, so extended-hours prints would not match).
"""

from __future__ import annotations

import logging
from typing import Callable

from tastytrade.dxfeed import Trade

from app.services.chart_data import ChartDataError, normalize_symbol
from app.services.market import get_market_status
from app.tastytrade.streamer import get_streamer

logger = logging.getLogger("trading.chart_live")

MAX_CHART_SYMBOLS = 12


class ChartLive:
    def __init__(self, streamer: Callable = get_streamer,
                 market_status: Callable[[], dict] = get_market_status) -> None:
        self._streamer = streamer
        self._market_status = market_status
        self._symbols: dict[str, list[str]] = {}

    async def set_symbols(self, conn_id: str, symbols: list) -> None:
        clean: list[str] = []
        for raw in symbols if isinstance(symbols, list) else []:
            if not isinstance(raw, str):
                continue
            try:
                sym = normalize_symbol(raw)
            except ChartDataError:
                continue
            if sym not in clean:
                clean.append(sym)
            if len(clean) >= MAX_CHART_SYMBOLS:
                break
        self._symbols[conn_id] = clean
        try:
            streamer = self._streamer()
            await streamer._ensure()
            for sym in clean:
                if not await streamer._subscribe(Trade, sym):
                    logger.debug("chart live subscribe failed for %s", sym)
        except Exception as exc:  # noqa: BLE001
            logger.debug("chart live subscribe error: %s", exc)

    def forget(self, conn_id: str) -> None:
        self._symbols.pop(conn_id, None)

    def prices(self, conn_id: str) -> dict[str, dict]:
        symbols = self._symbols.get(conn_id)
        if not symbols:
            return {}
        try:
            if self._market_status().get("session") != "regular":
                return {}
            streamer = self._streamer()
            out: dict[str, dict] = {}
            for sym in symbols:
                price, ts = streamer.cached_trade(sym)
                if price is not None and ts is not None:
                    out[sym] = {"price": float(price), "ts": int(ts * 1000)}
            return out
        except Exception as exc:  # noqa: BLE001
            logger.debug("chart live prices error: %s", exc)
            return {}


chart_live = ChartLive()
