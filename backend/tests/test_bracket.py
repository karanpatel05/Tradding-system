"""Bracket (OTOCO) execution tests — structure + paper gate + order cancel.

Run: python -m tests.test_bracket

These never transmit: `_build_otoco` is checked offline with a real `Leg` (so pydantic
validates the structure), the paper gate is the default, and `_cancel_open_orders`
runs against a fake account. The real transmit path is validated in the Tastytrade
cert env, not here.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tastytrade.order import (
    ComplexOrderType, InstrumentType, Leg, OrderAction, OrderType,
)

from app.engine.execution import (
    _build_otoco, _cancel_open_orders, _order_symbols, submit_bracket_order,
)


class FakeEquity:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def build_leg(self, quantity: Decimal, action: OrderAction) -> Leg:
        return Leg(instrument_type=InstrumentType.EQUITY, symbol=self.symbol,
                   action=action, quantity=quantity)


def _test_build_otoco() -> None:
    # LONG: market Buy-to-Open entry → OCO [Stop @stop Sell-to-Close, Limit @+target Sell-to-Close].
    co = _build_otoco(FakeEquity("AAA"), "long", 10, stop=98.0, target=104.0)
    assert co.type == ComplexOrderType.OTOCO, co.type
    assert co.trigger_order.order_type == OrderType.MARKET
    assert co.trigger_order.legs[0].action == OrderAction("Buy to Open")
    assert co.trigger_order.legs[0].quantity == Decimal("10")
    stop_o, target_o = co.orders
    assert stop_o.order_type == OrderType("Stop") and stop_o.stop_trigger == Decimal("98.0")
    assert stop_o.legs[0].action == OrderAction("Sell to Close")
    assert target_o.order_type == OrderType("Limit") and target_o.price == Decimal("104.0")
    assert target_o.legs[0].action == OrderAction("Sell to Close")
    print("PASS: long OTOCO = market entry + OCO(stop, +credit target), close = Sell to Close")

    # SHORT mirror: Sell-to-Open entry, Buy-to-Close exits, target price is a debit (−).
    co = _build_otoco(FakeEquity("BBB"), "short", 5, stop=110.0, target=92.0)
    assert co.trigger_order.legs[0].action == OrderAction("Sell to Open")
    stop_o, target_o = co.orders
    assert stop_o.stop_trigger == Decimal("110.0")
    assert stop_o.legs[0].action == OrderAction("Buy to Close")
    assert target_o.price == Decimal("-92.0"), target_o.price  # debit
    assert target_o.legs[0].action == OrderAction("Buy to Close")
    print("PASS: short OTOCO mirrors (Sell to Open / Buy to Close, −debit target)")


def _test_paper_gate() -> None:
    # ENABLE_LIVE_ORDERS is False by default → never transmits.
    out = asyncio.run(submit_bracket_order("NVDA", "long", 10, 100.0, 98.0, 104.0, reason="entry"))
    assert out["transmitted"] is False and out.get("paper") and out.get("bracket"), out
    assert out["stop"] == 98.0 and out["target"] == 104.0, out
    assert asyncio.run(submit_bracket_order("NVDA", "long", 0, 100.0, 98.0, 104.0))["transmitted"] is False
    print("PASS: submit_bracket_order paper-gated (no transmit, returns bracket intent)")


class _FakeOrder:
    def __init__(self, oid, symbol):
        self.id = oid
        self.legs = [Leg(instrument_type=InstrumentType.EQUITY, symbol=symbol,
                         action=OrderAction("Sell to Close"), quantity=Decimal("1"))]


class _FakeComplex:
    def __init__(self, oid, symbol):
        self.id = oid
        self.orders = [_FakeOrder(f"{oid}a", symbol), _FakeOrder(f"{oid}b", symbol)]
        self.trigger_order = None


class _FakeAccount:
    account_number = "TEST"

    def __init__(self):
        self.deleted: list = []
        self._complex = [_FakeComplex("c1", "AAA"), _FakeComplex("c2", "BBB")]
        self._orders = [_FakeOrder("o1", "AAA")]

    async def get_live_complex_orders(self, session): return self._complex
    async def get_live_orders(self, session): return self._orders
    async def delete_complex_order(self, session, oid): self.deleted.append(oid)
    async def delete_order(self, session, oid): self.deleted.append(oid)


def _test_order_symbols_and_cancel() -> None:
    assert _order_symbols(_FakeComplex("c1", "AAA")) == {"AAA"}
    assert _order_symbols(_FakeOrder("o1", "TSLA")) == {"TSLA"}

    # Symbol-scoped cancel: only AAA orders (1 complex + 1 simple) cancelled.
    acct = _FakeAccount()
    n = asyncio.run(_cancel_open_orders(None, acct, symbol="AAA"))
    assert n == 2 and set(acct.deleted) == {"c1", "o1"}, (n, acct.deleted)

    # Unscoped cancel-all: both complex + the simple order.
    acct2 = _FakeAccount()
    n2 = asyncio.run(_cancel_open_orders(None, acct2, symbol=None))
    assert n2 == 3 and set(acct2.deleted) == {"c1", "c2", "o1"}, (n2, acct2.deleted)
    print("PASS: _order_symbols + _cancel_open_orders (symbol-scoped and cancel-all)")


def _run() -> None:
    _test_build_otoco()
    _test_paper_gate()
    _test_order_symbols_and_cancel()
    print("\nALL BRACKET TESTS PASSED")


if __name__ == "__main__":
    _run()
