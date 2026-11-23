"""Tastytrade market data for the QQQ spread (data only; no orders ever go there).

* option quotes: the day's QQQ chain maps each unpadded OCC symbol to its DXLink
  streamer symbol; bid/ask come from the shared streamer cache with their receive time,
  so a stale quote is never used to price an order;
* minute bars: the shared candle collector (``marketdata.facade.bars``), the same
  Tastytrade candles the external runner reads.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime

logger = logging.getLogger("trading.qqq_spread.feed")

CHAIN_TIMEOUT = 15.0
MAX_QUOTE_AGE = 30.0     # seconds; older is no quote


class TastytradeSpreadFeed:
    def __init__(self, ticker: str = "QQQ") -> None:
        self.ticker = ticker
        self._streamer_syms: dict[date, dict[str, str]] = {}

    def _streamer(self):
        from app.tastytrade.streamer import get_streamer
        return get_streamer()

    async def _symbol_map(self, expiry: date) -> dict[str, str]:
        hit = self._streamer_syms.get(expiry)
        if hit:
            return hit
        from tastytrade.instruments import get_option_chain

        from app.tastytrade.session import get_tt_session
        sess = await get_tt_session()
        if sess is None:
            return {}
        try:
            chain = await asyncio.wait_for(get_option_chain(sess, self.ticker), CHAIN_TIMEOUT)
        except Exception:  # noqa: BLE001 - retryable
            logger.warning("QQQ chain lookup failed", exc_info=True)
            return {}
        out = {}
        for o in chain.get(expiry, []):
            if o.streamer_symbol and o.symbol:
                out[str(o.symbol).replace(" ", "")] = o.streamer_symbol
        if out:
            self._streamer_syms = {expiry: out}
        return out

    async def option_quotes(self, occs: list[str], expiry: date,
                            max_age: float = MAX_QUOTE_AGE) -> dict[str, dict]:
        """{occ: {bid, ask, mid}} for every symbol with a fresh, valid quote."""
        smap = await self._symbol_map(expiry)
        st = self._streamer()
        out = {}
        for occ in occs:
            sym = smap.get(occ)
            if not sym:
                continue
            try:
                await st.quote(sym, timeout=3.0)          # subscribes on first use
            except Exception:  # noqa: BLE001
                logger.warning("quote %s failed", sym, exc_info=True)
                continue
            q, ts = st.cached_quote(sym)
            if not q or ts is None or time.time() - ts > max_age:
                continue
            bid, ask = q.get("bid"), q.get("ask")
            if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
                continue
            out[occ] = {"bid": float(bid), "ask": float(ask),
                        "mid": round((float(bid) + float(ask)) / 2, 4)}
        return out

    async def minute_bars(self, start: datetime, end: datetime) -> list[dict]:
        from app.marketdata.facade import bars
        return await bars(self.ticker, "1m", start=start, end=end)
