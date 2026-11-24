"""Order execution interface for the XSP fly.

The runner talks to an ``Executor``: ``combo`` (the 4-leg entry), ``single`` (one leg:
stop closes, flattens, unwinds) and ``cancel_all``. The only executor that trades is
``alpaca_executor.AlpacaFlyExecutor`` (real orders on the Alpaca paper account). There is
no simulated executor: XSP never books a fill Alpaca didn't report.

``LiveExecutorUnavailable`` refuses every order: real-money trading is not supported,
and this app never sends orders to Tastytrade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.strategies.xsp_iron_fly import logic as L


@dataclass
class OrderResult:
    status: str                       # filled | partial | unfilled | rejected | unknown
    filled_qty: int = 0
    prices: dict[str, float] = field(default_factory=dict)   # role -> fill price
    mids: dict[str, float] = field(default_factory=dict)     # role -> mid at fill
    detail: str = ""
    order_id: str = ""                 # broker order id (recorded on each fill)


class Executor(Protocol):
    name: str
    supports_combo: bool

    async def combo(self, legs: list[dict], qty: int, limit_credit: float, *,
                    max_age: float | None = None) -> OrderResult: ...

    async def single(self, leg: dict, side: str, qty: int, limit: float, *,
                     max_age: float | None = None) -> OrderResult: ...

    async def cancel_all(self) -> int: ...


class LiveExecutorUnavailable:
    name = "live"
    supports_combo = True
    REASON = ("real-money XSP orders are not supported: this app routes orders only to "
              "Alpaca paper (XSP_FLY_MODE=alpaca_paper) and never sends orders to "
              "Tastytrade")

    async def combo(self, legs, qty, limit_credit, *, max_age=None) -> OrderResult:
        return OrderResult("rejected", detail=self.REASON)

    async def single(self, leg, side, qty, limit, *, max_age=None) -> OrderResult:
        return OrderResult("rejected", detail=self.REASON)

    async def cancel_all(self) -> int:
        return 0
