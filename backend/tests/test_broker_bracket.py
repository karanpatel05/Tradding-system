"""OTOCO construction is pure and exhaustively checked (this is where sign/direction
bugs lose money): long -> BUY entry + SELL tp/stop; short mirrored; tp is a LIMIT at
target, stop is a STOP with stop_trigger; paper path returns the same shape the worker
already consumes (transmitted False + stop/target echoed).

Deviation from the brief (binding per Task 2's resolution): `NewOrder`/`NewComplexOrder`
construction is fully pydantic-VALIDATED, never model_construct. A field typed
`list[Leg]` rejects a bare `SimpleNamespace` (pydantic has no `from_attributes` config
here, so it demands a real `Leg` instance or a dict) -- so `_leg_factory` below builds
REAL `tastytrade.order.Leg` objects, same pattern as test_broker_equity_orders.py's
`_wire` fixture, instead of the brief's `SimpleNamespace(symbol=..., action=..., quantity=...)`.
"""
from decimal import Decimal

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings


def _leg_factory(qty, action):
    from tastytrade.order import InstrumentType, Leg
    return Leg(instrument_type=InstrumentType.EQUITY, symbol="AAPL", action=action,
               quantity=Decimal(qty))


def test_build_otoco_long():
    from tastytrade.order import OrderAction, OrderType
    o = ord_mod.build_otoco("long", 10, target=110.0, stop=95.0, leg_factory=_leg_factory)
    assert str(o.trigger_order.legs[0].action) == str(OrderAction.BUY)
    tp, stp = o.orders
    assert tp.order_type == OrderType.LIMIT and float(tp.price) == 110.0
    assert str(tp.legs[0].action) == str(OrderAction.SELL)
    assert stp.order_type == OrderType.STOP and float(stp.stop_trigger) == 95.0
    assert str(stp.legs[0].action) == str(OrderAction.SELL)


def test_build_otoco_short_mirrored():
    from tastytrade.order import OrderAction
    o = ord_mod.build_otoco("short", 5, target=90.0, stop=105.0, leg_factory=_leg_factory)
    assert str(o.trigger_order.legs[0].action) == str(OrderAction.SELL)
    assert all(str(child.legs[0].action) == str(OrderAction.BUY) for child in o.orders)


def test_build_otoco_bad_side():
    with pytest.raises(ValueError):
        ord_mod.build_otoco("sideways", 5, target=1, stop=2, leg_factory=_leg_factory)


@pytest.mark.asyncio
async def test_paper_bracket_shape(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def mid(symbol):
        return 100.0
    monkeypatch.setattr(ord_mod, "_mid", mid)
    out = await ord_mod.submit_bracket_order("AAPL", "long", 10, 100.0, 95.0, 110.0)
    assert out["transmitted"] is False and out["paper"] is True and out["bracket"] is True
    assert out["stop"] == 95.0 and out["target"] == 110.0


@pytest.mark.asyncio
async def test_paper_poll_and_cancel(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    assert await ord_mod.poll_bracket_fill("AAPL") is None      # worker simulates fills
    out = await ord_mod.cancel_symbol_orders("AAPL")
    assert out == {"cancelled": 0, "paper": True}
