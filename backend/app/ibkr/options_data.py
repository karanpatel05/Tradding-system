"""IBKR-side market-data helpers, extracted from the TC LivePriceSource so the
marketdata facade can use IBKR as a fallback without importing strategy code.
Pure extraction - logic identical to the original live_source implementations."""
from __future__ import annotations

import asyncio

from app.ibkr.contracts import index, option, stock
from app.ibkr.gateway import get_gateway
from app.ibkr.market import get_quotes as ib_stock_quotes  # re-export  # noqa: F401
from app.ibkr.market import normalize_ticker


async def ib_option_quote(underlying: str, expiry, strike: float, right: str) -> dict | None:
    ib = await get_gateway().get_ib()
    c = option(underlying, expiry, strike, right)
    await ib.qualifyContractsAsync(c)
    # qualify silently leaves conId=0 for a strike/expiry that doesn't exist (IBKR returns
    # "Unknown contract" without raising). Degrade to "no quote" instead of a 500.
    if not getattr(c, "conId", 0):
        return None
    t = ib.reqMktData(c, "", False, False)
    await asyncio.sleep(2)
    q = normalize_ticker(t)
    ib.cancelMktData(c)
    return q


async def ib_vix(symbol: str = "VIX") -> float | None:
    """Live VIX index level (CBOE). None if unavailable - callers fail closed."""
    ib = await get_gateway().get_ib()
    c = index(symbol)
    await ib.qualifyContractsAsync(c)
    t = ib.reqMktData(c, "", False, False)
    await asyncio.sleep(2)
    q = normalize_ticker(t)
    ib.cancelMktData(c)
    return q["price"]


async def ib_chain_strikes(underlying: str) -> list[float]:
    # Strikes are returned across all expirations by reqSecDefOptParams; no per-expiry arg.
    ib = await get_gateway().get_ib()
    u = stock(underlying)
    await ib.qualifyContractsAsync(u)
    chains = await ib.reqSecDefOptParamsAsync(u.symbol, "", u.secType, u.conId)
    for ch in chains:
        if ch.exchange == "SMART":
            return sorted(float(s) for s in ch.strikes)
    return sorted(float(s) for s in chains[0].strikes) if chains else []
