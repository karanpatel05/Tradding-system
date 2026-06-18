"""Position sizing — shared by backtest and live for parity.

Per the 9/15 EMA scalping spec, sizing is **capital / price** (not risk-fractional):

    shares = floor(allocated_capital / entry_price)   # whole shares only

A trade is rejected (0 shares, with a recorded reason) when one share already
costs more than the allocated capital. Risk still defines the stop/target
distance in the signal core — it just doesn't size the position here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizingResult:
    quantity: int
    notional: float
    capped_by: str          # 'buying_power' | 'none'
    rejection: str = ""     # non-empty when quantity == 0 for a real reason


def size_position(allocation: float, entry: float) -> SizingResult:
    """Whole-share quantity = floor(allocation / entry). Rejects unaffordable
    trades (one share costs more than the allocation) and bad inputs."""
    if allocation <= 0 or entry <= 0:
        return SizingResult(0, 0.0, "none", "invalid allocation/price")
    if entry > allocation:
        return SizingResult(0, 0.0, "buying_power",
                            f"entry_price {entry:.2f} exceeds allocated_capital {allocation:.2f}")
    qty = int(math.floor(allocation / entry))
    if qty <= 0:
        return SizingResult(0, 0.0, "buying_power", "allocation buys 0 whole shares")
    return SizingResult(qty, qty * entry, "buying_power", "")
