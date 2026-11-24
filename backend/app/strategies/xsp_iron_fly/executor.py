"""Order execution for the XSP fly.

``SimExecutor`` is the only executor that fills. It simulates MARKETABLE LIMIT orders
against the real NBBO from the feed:

* every order is immediate-or-cancel: a limit that is marketable against the current
  touch fills at the touch (BUY at the ask, SELL at the bid); otherwise nothing fills
  and nothing rests, so there are never working orders left behind;
* a 4-leg combo fills only when its net credit limit is at or below the natural credit
  (shorts at the bid, wings at the ask): pessimistic on purpose, because live XSP fills
  can be worse than any model;
* a quote older than the caller's ``max_age`` (or with no receive time) is no touch at
  all: the order does not fill. A feed stall must never book paper fills at
  pre-outage prices.

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
    status: str                       # filled | partial | unfilled | rejected | unknown
    filled_qty: int = 0
    prices: dict[str, float] = field(default_factory=dict)   # role -> fill price
    mids: dict[str, float] = field(default_factory=dict)     # role -> mid at fill
    detail: str = ""


class Executor(Protocol):
    name: str
    supports_combo: bool

    async def combo(self, legs: list[dict], qty: int, limit_credit: float, *,
                    max_age: float | None = None) -> OrderResult: ...

    async def single(self, leg: dict, side: str, qty: int, limit: float, *,
                     max_age: float | None = None) -> OrderResult: ...

    async def cancel_all(self) -> int: ...


class SimExecutor:
    name = "sim"

    def __init__(self, feed, clock, *, supports_combo: bool = True,
                 max_fill_qty: int | None = None) -> None:
        self.feed = feed
        self.clock = clock                    # quote ages are measured on this clock
        self.supports_combo = supports_combo
        self.max_fill_qty = max_fill_qty      # simulate partial fills (tests)

    def _touch(self, symbol: str, max_age: float | None) -> tuple[float, float] | str:
        """(bid, ask), or why there is no usable touch."""
        q = self.feed.quote(symbol)
        if q is None or not L.quote_valid(q.bid, q.ask):
            return f"no quote for {symbol}"
        if max_age is not None:
            age = q.age(self.clock.now())
            if age is None or age > max_age:
                return f"stale quote for {symbol} (age {age}s > {max_age:g}s)"
        return q.bid, q.ask

    def _fillable(self, qty: int) -> int:
        return qty if self.max_fill_qty is None else min(qty, self.max_fill_qty)

    async def combo(self, legs: list[dict], qty: int, limit_credit: float, *,
                    max_age: float | None = None) -> OrderResult:
        touches = {}
        for leg in legs:
            t = self._touch(leg["symbol"], max_age)
            if isinstance(t, str):
                return OrderResult("unfilled", detail=t)
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

    async def single(self, leg: dict, side: str, qty: int, limit: float, *,
                     max_age: float | None = None) -> OrderResult:
        t = self._touch(leg["symbol"], max_age)
        if isinstance(t, str):
            return OrderResult("unfilled", detail=t)
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
    REASON = ("real-money XSP orders are not supported: this app routes orders only to "
              "Alpaca paper (XSP_FLY_MODE=alpaca_paper) and never sends orders to "
              "Tastytrade")

    async def combo(self, legs, qty, limit_credit, *, max_age=None) -> OrderResult:
        return OrderResult("rejected", detail=self.REASON)

    async def single(self, leg, side, qty, limit, *, max_age=None) -> OrderResult:
        return OrderResult("rejected", detail=self.REASON)

    async def cancel_all(self) -> int:
        return 0
