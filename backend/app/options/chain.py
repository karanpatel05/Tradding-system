"""Read-only IBKR access for the builder: the underlying's option chain (available expiries
+ strikes) and per-leg live mids. No orders — quotes/definitions only."""
from __future__ import annotations

import asyncio
from datetime import date

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.strategies.triple_calendar.live_source import LivePriceSource


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


async def get_option_chain(underlying: str) -> dict:
    ib = await get_gateway().get_ib()
    u = stock(underlying)
    await ib.qualifyContractsAsync(u)
    chains = await ib.reqSecDefOptParamsAsync(u.symbol, "", u.secType, u.conId)
    ch = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
    if ch is None:
        return {"underlying": underlying.upper(), "expirations": [], "strikes": []}
    return {
        "underlying": underlying.upper(),
        "expirations": sorted(_iso(str(e)) for e in ch.expirations),
        "strikes": sorted(float(s) for s in ch.strikes),
    }


async def get_leg_mids(underlying: str, legs: list[dict]) -> list[float | None]:
    src = LivePriceSource()
    return list(await asyncio.gather(*[
        src.aoption_mid(underlying, date.fromisoformat(leg["expiry"]),
                        leg["strike"], leg["right"])
        for leg in legs
    ]))
