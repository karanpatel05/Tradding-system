"""Live quotes over IBKR (reqMktData). Normalizer kept pure for unit testing."""
from __future__ import annotations

import asyncio
import math

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def normalize_ticker(t) -> dict:
    bid, ask, last, close = _num(t.bid), _num(t.ask), _num(t.last), _num(t.close)
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = last if last is not None else mid if mid is not None else close
    return {"bid": bid, "ask": ask, "last": last, "close": close, "price": price}


async def get_quotes(symbols: list[str], timeout: float = 3.0) -> dict[str, dict]:
    ib = await get_gateway().get_ib()
    contracts = {sym: stock(sym) for sym in symbols}
    # reqMktData needs a qualified contract (conId) — otherwise ib_async can't hash it.
    await ib.qualifyContractsAsync(*contracts.values())
    tickers = {sym: ib.reqMktData(contracts[sym], "", False, False) for sym in symbols}
    await asyncio.sleep(timeout)   # let ticks arrive
    out = {sym: normalize_ticker(t) for sym, t in tickers.items()}
    for sym in symbols:
        ib.cancelMktData(contracts[sym])
    return out
