"""IBKR contract helpers. One place that builds the contracts used across market
data, history, and execution so routing (SMART/USD) stays consistent."""
from __future__ import annotations

from datetime import date

from ib_async import ComboLeg, Contract, Index, Option, Stock


def stock(symbol: str) -> Stock:
    return Stock(symbol.upper(), "SMART", "USD")


def index(symbol: str, exchange: str = "CBOE") -> Index:
    """Cash index (e.g. VIX on CBOE) — used for read-only quotes, not tradable here."""
    return Index(symbol.upper(), exchange, "USD")


def option(underlying: str, expiry: date, strike: float, right: str) -> Option:
    return Option(underlying.upper(), expiry.strftime("%Y%m%d"), float(strike),
                  right.upper(), "SMART")


def combo(underlying: str, legs: list[tuple[int, str]]) -> Contract:
    """BAG combo from (conId, action) legs (ratio 1 each)."""
    c = Contract(symbol=underlying.upper(), secType="BAG", currency="USD", exchange="SMART")
    c.comboLegs = [ComboLeg(conId=cid, ratio=1, action=act, exchange="SMART")
                   for cid, act in legs]
    return c
