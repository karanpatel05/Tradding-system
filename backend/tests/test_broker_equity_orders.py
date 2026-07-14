"""Equity submit: the gate is absolute (paper path may not touch the order API — the
fake RAISES if it does); live path validates with dry_run BEFORE placing; intent→action
mapping; SAFE_MODE blocks. No network."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings


class _Recorder:
    def __init__(self, dry_run_fails=False):
        self.calls = []
        self.dry_run_fails = dry_run_fails

    async def place_order(self, session, order, dry_run=True):
        self.calls.append(("place_order", dry_run,
                           [(l.symbol, str(l.action), int(l.quantity)) for l in order.legs]))
        if dry_run and self.dry_run_fails:
            raise RuntimeError("insufficient buying power")
        return SimpleNamespace(order=SimpleNamespace(id=42))


class _Bomb:
    async def place_order(self, *a, **k):
        raise AssertionError("order API touched in paper mode")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    async def mid(symbol):
        return 101.25
    monkeypatch.setattr(ord_mod, "_mid", mid)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    # Fake instruments return REAL SDK Leg objects so NewOrder's pydantic validation
    # stays ON (production never bypasses it).
    from tastytrade.order import InstrumentType, Leg

    def _leg(sym, qty, action):
        return Leg(instrument_type=InstrumentType.EQUITY, symbol=sym, action=action,
                   quantity=Decimal(qty))

    async def fake_equity_get(session, symbols):
        sym = list(symbols)[0]
        return [SimpleNamespace(symbol=sym,
                                build_leg=lambda qty, action: _leg(sym, qty, action))]
    monkeypatch.setattr(ord_mod, "_equities", fake_equity_get)


@pytest.mark.asyncio
async def test_paper_never_touches_api(monkeypatch):
    async def bomb():
        return _Bomb()
    monkeypatch.setattr(ord_mod, "_account", bomb)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 10, reason="t")
    assert out["transmitted"] is False and out["paper"] is True
    assert out["fill_price"] == 101.25          # simulated at the facade mid


@pytest.mark.asyncio
async def test_paper_no_mid_no_fill(monkeypatch):
    async def bomb():
        return _Bomb()
    monkeypatch.setattr(ord_mod, "_account", bomb)
    async def none_mid(symbol):
        return None
    monkeypatch.setattr(ord_mod, "_mid", none_mid)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 10)
    assert out["transmitted"] is False and out.get("fill_price") is None
    assert "no quote" in out["detail"]


@pytest.mark.asyncio
async def test_live_dry_run_first_then_place(monkeypatch):
    from app.db.base import init_db
    await init_db()          # record_intent writes to order_intents
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    rec = _Recorder()
    async def acct():
        return rec
    monkeypatch.setattr(ord_mod, "_account", acct)
    out = await ord_mod.submit_equity_order("AAPL", "exit_short", 5, reason="t")
    assert out["transmitted"] is True and out["order_id"] == 42
    kinds = [(c[0], c[1]) for c in rec.calls]
    assert kinds == [("place_order", True), ("place_order", False)]   # dry-run FIRST
    from tastytrade.order import OrderAction
    # Version-proof: compare against the SDK's own str form ("Buy" on 12.4.1 StrEnum).
    assert rec.calls[0][2] == [("AAPL", str(OrderAction.BUY), 5)]     # exit_short -> BUY


@pytest.mark.asyncio
async def test_live_dry_run_failure_rejects_locally(monkeypatch):
    from app.db.base import init_db
    await init_db()
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    rec = _Recorder(dry_run_fails=True)
    async def acct():
        return rec
    monkeypatch.setattr(ord_mod, "_account", acct)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and "rejected" in out["detail"]
    assert [(c[0], c[1]) for c in rec.calls] == [("place_order", True)]  # never placed


@pytest.mark.asyncio
async def test_safe_mode_blocks(monkeypatch):
    async def safe():
        return True
    monkeypatch.setattr(ord_mod, "is_safe_mode", safe)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and out.get("safe_mode") is True


@pytest.mark.asyncio
async def test_intent_mapping():
    assert ord_mod.intent_to_action("enter_long") == "BUY"
    assert ord_mod.intent_to_action("exit_short") == "BUY"
    assert ord_mod.intent_to_action("enter_short") == "SELL"
    assert ord_mod.intent_to_action("exit_long") == "SELL"
    with pytest.raises(ValueError):
        ord_mod.intent_to_action("nope")
