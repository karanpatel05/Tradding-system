"""Live option/stock price accessors used by the options Builder chain
(app/options/chain.py), served by the marketdata facade. Tastytrade is the market-data
source only; orders go to Alpaca paper."""
from __future__ import annotations

from app.marketdata import facade


class LivePriceSource:
    async def aunderlying(self, symbol: str) -> float | None:
        q = await facade.stock_quote(symbol)
        return q["price"] if q else None

    async def avix(self, symbol: str = "VIX") -> float | None:
        """VIX level via the facade. None if unavailable - the caller fails closed."""
        return await facade.vix()

    async def aoption_mid(self, underlying: str, expiry, strike: float, right: str) -> float | None:
        return await facade.option_mid(underlying, expiry, strike, right)

    async def achain(self, underlying: str) -> list[float]:
        return await facade.chain_strikes(underlying)
