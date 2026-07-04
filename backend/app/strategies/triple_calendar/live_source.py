"""IBKR-backed price accessors for live Triple Calendar trading. The Phase-1 core
needs SYNC reads; run_live builds a per-tick snapshot (see _Snapshot in
strategy_class) from these async accessors, so the core stays unchanged and never
does I/O. Needs OPRA real-time option data + market open for live quotes."""
from __future__ import annotations

import asyncio

from app.ibkr.contracts import index, option, stock
from app.ibkr.gateway import get_gateway
from app.ibkr.market import get_quotes, normalize_ticker


class LivePriceSource:
    async def aunderlying(self, symbol: str) -> float | None:
        return (await get_quotes([symbol]))[symbol]["price"]

    async def avix(self, symbol: str = "VIX") -> float | None:
        """Live VIX index level (CBOE). None if unavailable — the caller fails closed."""
        ib = await get_gateway().get_ib()
        c = index(symbol)
        await ib.qualifyContractsAsync(c)
        t = ib.reqMktData(c, "", False, False)
        await asyncio.sleep(2)
        q = normalize_ticker(t)
        ib.cancelMktData(c)
        return q["price"]

    async def aoption_mid(self, underlying: str, expiry, strike: float, right: str) -> float | None:
        ib = await get_gateway().get_ib()
        c = option(underlying, expiry, strike, right)
        await ib.qualifyContractsAsync(c)
        t = ib.reqMktData(c, "", False, False)
        await asyncio.sleep(2)
        q = normalize_ticker(t)
        ib.cancelMktData(c)
        return q["price"]

    async def achain(self, underlying: str) -> list[float]:
        # Strikes are returned across all expirations by reqSecDefOptParams; no per-expiry arg.
        ib = await get_gateway().get_ib()
        u = stock(underlying)
        await ib.qualifyContractsAsync(u)
        chains = await ib.reqSecDefOptParamsAsync(u.symbol, "", u.secType, u.conId)
        for ch in chains:
            if ch.exchange == "SMART":
                return sorted(float(s) for s in ch.strikes)
        return sorted(float(s) for s in chains[0].strikes) if chains else []
