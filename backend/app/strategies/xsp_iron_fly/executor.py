"""Order execution for the XSP fly.

``SimExecutor`` is the only executor that fills. It simulates MARKETABLE LIMIT orders
against the real NBBO from the feed:

* every order is immediate-or-cancel: a limit that is marketable against the current
  touch fills at the touch (BUY at the ask, SELL at the bid); otherwise nothing fills
  and nothing rests, so there are never working orders left behind;
* a 4-leg combo fills only when its net credit limit is at or below the natural credit
  (shorts at the bid, wings at the ask): pessimistic on purpose, because live XSP fills
  can be worse than any model.

``LiveExecutorUnavailable`` refuses every order. There is no live XSP order route in
this app (Alpaca paper is the only order path, and this app never sends orders to
Tastytrade), so live mode stays refused until an adapter is built and verified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.strategies.xsp_iron_fly import logic as L


@dataclass
class OrderResult:
    status: str                       # filled | partial | unfilled | rejected
    filled_qty: int = 0
    prices: dict[str, float] = field(default_factory=dict)   # role -> fill price
    mids: dict[str, float] = field(default_factory=dict)     # role -> mid at fill
    detail: str = ""


class Executor(Protocol):
    name: str
    supports_combo: bool

    async def combo(self, legs: list[dict], qty: int, limit_credit: float) -> OrderResult: ...

    async def single(self, leg: dict, side: str, qty: int, limit: float) -> OrderResult: ...

    async def cancel_all(self) -> int: ...


class SimExecutor:
    name = "sim"

    def __init__(self, feed, *, supports_combo: bool = True,
                 max_fill_qty: int | None = None) -> None:
        self.feed = feed
        self.supports_combo = supports_combo
        self.max_fill_qty = max_fill_qty      # simulate partial fills (tests)

    def _touch(self, symbol: str) -> tuple[float, float] | None:
        q = self.feed.quote(symbol)
        if q is None or not L.quote_valid(q.bid, q.ask):
            return None
        return q.bid, q.ask

    def _fillable(self, qty: int) -> int:
        return qty if self.max_fill_qty is None else min(qty, self.max_fill_qty)

    async def combo(self, legs: list[dict], qty: int, limit_credit: float) -> OrderResult:
        touches = {}
        for leg in legs:
            t = self._touch(leg["symbol"])
            if t is None:
                return OrderResult("unfilled", detail=f"no quote for {leg['symbol']}")
            touches[leg["role"]] = t
        _mid_c, nat_c = L.combo_credits(touches)
        if nat_c + L.EPS < limit_credit:
            return OrderResult("unfilled", detail=f"limit {limit_credit:.2f} > natural "
                                                  f"{nat_c:.2f}")
        n = self._fillable(qty)
        prices = {r: (b if r in L.SHORT_ROLES else a) for r, (b, a) in touches.items()}
        mids = {r: L.mid(b, a) for r, (b, a) in touches.items()}
        return OrderResult("filled" if n == qty else "partial", n, prices, mids,
                           f"sim combo {n}/{qty} @ natural {nat_c:.2f}")

    async def single(self, leg: dict, side: str, qty: int, limit: float) -> OrderResult:
        t = self._touch(leg["symbol"])
        if t is None:
            return OrderResult("unfilled", detail=f"no quote for {leg['symbol']}")
        bid, ask = t
        if side == "BUY":
            ok, px = limit + L.EPS >= ask, ask
        else:
            ok, px = bid > 0 and limit - L.EPS <= bid, bid
        if not ok:
            return OrderResult("unfilled", detail=f"{side} limit {limit:.2f} not marketable "
                                                  f"({bid:.2f}/{ask:.2f})")
        n = self._fillable(qty)
        return OrderResult("filled" if n == qty else "partial", n, {leg["role"]: px},
                           {leg["role"]: L.mid(bid, ask)}, f"sim {side} {n}/{qty} @ {px:.2f}")

    async def cancel_all(self) -> int:
        return 0          # IOC simulation: nothing ever rests


class LiveExecutorUnavailable:
    name = "live"
    supports_combo = True
    REASON = ("no live XSP order route is built: Alpaca paper is this app's only order "
              "path and has no verified XSP index-option support, and this app never "
              "sends orders to Tastytrade")

    async def combo(self, legs, qty, limit_credit) -> OrderResult:
        return OrderResult("rejected", detail=self.REASON)

    async def single(self, leg, side, qty, limit) -> OrderResult:
        return OrderResult("rejected", detail=self.REASON)

    async def cancel_all(self) -> int:
        return 0
