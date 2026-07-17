"""Broker account reads: cached Account threading, normalized balances/positions,
never-raise degradation. All SDK objects faked — no network."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import account as acct


class _FakeAccount:
    account_number = "5WT00000"

    async def get_balances(self, session, currency="USD"):
        return SimpleNamespace(
            net_liquidating_value=Decimal("25000.50"), cash_balance=Decimal("10000"),
            equity_buying_power=Decimal("50000"), derivative_buying_power=Decimal("20000"),
            margin_equity=Decimal("25000.50"))

    async def get_positions(self, session, include_marks=None):
        return [SimpleNamespace(symbol="AAPL", quantity=Decimal("10"),
                                quantity_direction="Long", instrument_type="Equity",
                                average_open_price=Decimal("150.25"),
                                account_number="5WT00000", multiplier=1),
                SimpleNamespace(symbol="QQQ   260807C00725000", quantity=Decimal("0"),
                                quantity_direction="Zero", instrument_type="Equity Option",
                                average_open_price=Decimal("1"),
                                account_number="5WT00000", multiplier=100)]


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    async def fake_session():
        return object()
    monkeypatch.setattr(acct, "_get_session", fake_session)
    async def fake_fetch(session):
        return _FakeAccount()
    monkeypatch.setattr(acct, "_fetch_account", fake_fetch)
    acct.reset_tt_account()
    yield
    acct.reset_tt_account()


@pytest.mark.asyncio
async def test_account_cached_and_reset():
    a1 = await acct.get_tt_account()
    a2 = await acct.get_tt_account()
    assert a1 is a2 and a1.account_number == "5WT00000"
    acct.reset_tt_account()
    assert (await acct.get_tt_account()) is not a1


@pytest.mark.asyncio
async def test_balances_normalized_shape():
    b = await acct.balances()
    assert b["net_liquidation"] == 25000.50
    assert b["total_cash"] == 10000.0
    assert b["buying_power"] == 50000.0
    assert b["account"] == "5WT00000" and b["currency"] == "USD"
    for k in ("available_funds", "gross_position_value", "unrealized_pnl",
              "realized_pnl", "init_margin", "maint_margin"):
        assert k in b   # keys exist (None where TT has no equivalent)


@pytest.mark.asyncio
async def test_positions_normalized_nonzero_only():
    ps = await acct.positions()
    assert len(ps) == 1
    p = ps[0]
    assert (p.symbol, p.quantity, p.quantity_direction, p.instrument_type,
            p.avg_price, p.account) == ("AAPL", 10, "Long", "Equity", 150.25, "5WT00000")


@pytest.mark.asyncio
async def test_no_session_degrades(monkeypatch):
    async def none_session():
        return None
    monkeypatch.setattr(acct, "_get_session", none_session)
    acct.reset_tt_account()
    assert await acct.get_tt_account() is None
    assert await acct.balances() == {}
    assert await acct.positions() == []
