"""submit_combo_order passes `quantity` through to every leg of the placed order.
(Repointed from the old IBKR internals to the broker layer when execution.py became
a re-export — same behavioral assertion as before the swap.)"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings
from app.engine import execution


@pytest.mark.asyncio
async def test_submit_combo_order_uses_quantity(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    placed = []
    async def place_order(session, order, dry_run=True):
        placed.append((dry_run, [int(l.quantity) for l in order.legs]))
        return SimpleNamespace(order=SimpleNamespace(id=7))
    async def acct():
        return SimpleNamespace(place_order=place_order)
    monkeypatch.setattr(ord_mod, "_account", acct)
    from tastytrade.order import InstrumentType, Leg
    async def fake_option(session, occ):
        return SimpleNamespace(symbol=occ, build_leg=lambda qty, action: Leg(
            instrument_type=InstrumentType.EQUITY_OPTION, symbol=occ, action=action,
            quantity=Decimal(qty)))
    monkeypatch.setattr(ord_mod, "_option", fake_option)
    async def held():
        return set()
    monkeypatch.setattr(ord_mod, "_held_symbols", held)

    out = await execution.submit_combo_order("QQQ", [
        {"expiry": date(2026, 8, 7), "strike": 725.0, "right": "C", "action": "BUY"},
        {"expiry": date(2026, 9, 4), "strike": 725.0, "right": "C", "action": "SELL"},
    ], net_price=1.5, quantity=3)
    assert out["transmitted"] is True
    real = [p for p in placed if p[0] is False]
    assert real and real[0][1] == [3, 3]       # quantity=3 on every leg
