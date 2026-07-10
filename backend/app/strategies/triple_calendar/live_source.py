"""Price accessors for live Triple Calendar trading. The Phase-1 core needs SYNC reads;
run_live builds a per-tick snapshot (see _Snapshot in strategy_class) from these async
accessors, so the core stays unchanged and never does I/O. (Rewired to the marketdata
facade in the Tastytrade migration; here still IBKR helpers, extraction step.)"""
from __future__ import annotations

from app.ibkr.options_data import ib_chain_strikes, ib_option_quote, ib_stock_quotes, ib_vix


class LivePriceSource:
    async def aunderlying(self, symbol: str) -> float | None:
        return (await ib_stock_quotes([symbol]))[symbol]["price"]

    async def avix(self, symbol: str = "VIX") -> float | None:
        """Live VIX index level. None if unavailable - the caller fails closed."""
        return await ib_vix(symbol)

    async def aoption_mid(self, underlying: str, expiry, strike: float, right: str) -> float | None:
        q = await ib_option_quote(underlying, expiry, strike, right)
        return q["price"] if q else None

    async def achain(self, underlying: str) -> list[float]:
        return await ib_chain_strikes(underlying)
