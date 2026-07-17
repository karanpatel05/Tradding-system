"""Combo submit: OCC symbol construction (exact padding), per-leg TO_OPEN/TO_CLOSE from
the positions lookup, net-debit price sign (negative Decimal = pay), paper path never
touches the API.

Note on enum-string assertions: tastytrade.order.OrderAction is a StrEnum whose *value*
(and therefore str()) is a human-readable phrase, e.g. str(OrderAction.SELL_TO_CLOSE) ==
"Sell to Close" (verified against the installed SDK: BUY_TO_OPEN='Buy to Open',
BUY_TO_CLOSE='Buy to Close', SELL_TO_OPEN='Sell to Open', SELL_TO_CLOSE='Sell to Close',
BUY='Buy', SELL='Sell'). The membership checks below therefore compare against the
enum member's `.name` (e.g. "SELL_TO_CLOSE"), not `str()` of the member.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings


def test_occ_symbol_padding():
    # OCC: 6-char root, yymmdd, C/P, strike*1000 as 8 digits.
    assert ord_mod.occ_symbol_for("QQQ", date(2026, 8, 7), 725.0, "C") == "QQQ   260807C00725000"
    assert ord_mod.occ_symbol_for("SPY", date(2026, 12, 18), 82.5, "P") == "SPY   261218P00082500"
    assert ord_mod.occ_symbol_for("BRKB", date(2026, 8, 7), 1000.0, "C") == "BRKB  260807C01000000"


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    rec = SimpleNamespace(calls=[])
    async def place_order(session, order, dry_run=True):
        rec.calls.append((dry_run, [(l.symbol, l.action.name, int(l.quantity)) for l in order.legs],
                          order.price))
        return SimpleNamespace(order=SimpleNamespace(id=7))
    async def acct():
        return SimpleNamespace(place_order=place_order)
    monkeypatch.setattr(ord_mod, "_account", acct)
    # Fake instrument returns a REAL SDK Leg (InstrumentType.EQUITY_OPTION) so NewOrder's
    # pydantic validation stays in force — a bare SimpleNamespace leg is rejected by
    # `list[Leg]` (pydantic has no from_attributes config here; see
    # test_broker_equity_orders.py / test_broker_bracket.py for the same pattern).
    from tastytrade.order import InstrumentType, Leg
    def _leg(occ, qty, action):
        return Leg(instrument_type=InstrumentType.EQUITY_OPTION, symbol=occ, action=action,
                  quantity=qty)
    async def fake_options(session, occ):
        return SimpleNamespace(symbol=occ, build_leg=lambda qty, action: _leg(occ, qty, action))
    monkeypatch.setattr(ord_mod, "_option", fake_options)
    async def held():
        return {"QQQ   260807C00725000"}      # already long this one
    monkeypatch.setattr(ord_mod, "_held_symbols", held)
    return rec


@pytest.mark.asyncio
async def test_combo_actions_and_price(wired):
    legs = [
        {"expiry": date(2026, 8, 7), "strike": 725.0, "right": "C", "action": "SELL"},
        {"expiry": date(2026, 9, 4), "strike": 725.0, "right": "C", "action": "BUY"},
    ]
    out = await ord_mod.submit_combo_order("QQQ", legs, net_price=1.85, action="BUY",
                                           quantity=2)
    assert out["transmitted"] is True and out["legs"] == 2 and out["order_id"] == 7
    dry, placed = wired.calls
    assert dry[0] is True and placed[0] is False                 # dry-run first
    (s1, a1, q1), (s2, a2, q2) = placed[1]
    assert s1 == "QQQ   260807C00725000" and "SELL_TO_CLOSE" in a1 and q1 == 2  # held -> TO_CLOSE
    assert s2 == "QQQ   260904C00725000" and "BUY_TO_OPEN" in a2 and q2 == 2    # not held -> TO_OPEN
    assert placed[2] == Decimal("-1.85")     # net debit -> negative (pay)


@pytest.mark.asyncio
async def test_combo_paper_never_touches_api(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def bomb():
        raise AssertionError("touched")
    monkeypatch.setattr(ord_mod, "_account", bomb)
    out = await ord_mod.submit_combo_order("QQQ", [
        {"expiry": date(2026, 8, 7), "strike": 725.0, "right": "C", "action": "BUY"}],
        net_price=2.0)
    assert out["transmitted"] is False and out["paper"] is True
