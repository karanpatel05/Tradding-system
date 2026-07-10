"""Read-only IBKR access for the builder: the underlying's option chain (available expiries
+ strikes) and per-leg live mids. No orders — quotes/definitions only."""
from __future__ import annotations

import asyncio
from datetime import date

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.marketdata import facade
from app.strategies.triple_calendar.live_source import LivePriceSource


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


async def _ib_option_chain(underlying: str) -> dict:
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


async def get_option_chain(underlying: str) -> dict:
    expirations = await facade.chain_expiries(underlying)
    strikes = await facade.chain_strikes(underlying)
    if expirations and strikes:
        return {"underlying": underlying.upper(),
                "expirations": expirations, "strikes": strikes}
    return await _ib_option_chain(underlying)


async def get_leg_mids(underlying: str, legs: list[dict]) -> list[float | None]:
    src = LivePriceSource()
    return list(await asyncio.gather(*[
        src.aoption_mid(underlying, date.fromisoformat(leg["expiry"]),
                        leg["strike"], leg["right"])
        for leg in legs
    ]))


async def get_chain_quotes(underlying: str, expiry: str, count: int = 8) -> dict:
    """Near-ATM quoted slice for ONE expiry: call+put mids for `count` strikes each side of the
    ATM strike, fetched concurrently. Read-only. Mids are None when unavailable; atm/rows are
    empty when the underlying has no usable spot (IBKR returns -1 with no live quote)."""
    q = await facade.stock_quote(underlying)
    spot = (q or {}).get("price")
    chain = await get_option_chain(underlying)
    strikes = chain["strikes"]
    base = {"underlying": underlying.upper(), "expiry": expiry, "spot": spot,
            "atm": None, "count": count, "rows": []}
    if not strikes or spot is None or spot <= 0:
        return base
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    window = strikes[max(0, i - count): i + count + 1]
    exp = date.fromisoformat(expiry)
    src = LivePriceSource()
    tasks = []
    for s in window:
        tasks.append(src.aoption_mid(underlying, exp, s, "C"))
        tasks.append(src.aoption_mid(underlying, exp, s, "P"))
    mids = await asyncio.gather(*tasks)
    rows = [{"strike": s, "call_mid": mids[2 * idx], "put_mid": mids[2 * idx + 1]}
            for idx, s in enumerate(window)]
    return {**base, "atm": atm, "rows": rows}
