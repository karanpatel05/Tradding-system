"""IBKR contract helpers. One place that builds the Stock contracts used across
market data, history, and execution so routing (SMART/USD) stays consistent."""
from __future__ import annotations

from ib_async import Stock


def stock(symbol: str) -> Stock:
    return Stock(symbol.upper(), "SMART", "USD")
