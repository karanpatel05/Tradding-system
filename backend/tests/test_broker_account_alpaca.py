"""Alpaca-backed account views: app-owned only, masked account, degrade on error."""
from decimal import Decimal

import httpx
import pytest

from app.broker import account as acct
from app.broker import alpaca_client as ac
from app.config import get_settings


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "PK")
    monkeypatch.setattr(s, "apca_api_secret_key", "sk")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")


def _mock(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


ACCOUNT = {"account_number": "PA12345678", "equity": "1000.5", "cash": "400",
           "buying_power": "2000"}


@pytest.mark.asyncio
async def test_balances_masked_and_mapped(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(200, json=ACCOUNT))
    b = await acct.balances()
    assert b["net_liquidation"] == 1000.5 and b["total_cash"] == 400.0
    assert b["buying_power"] == 2000.0 and b["available_funds"] == 2000.0
    assert b["account"] == "••••5678" and b["currency"] == "USD"
    assert b["unrealized_pnl"] is None


@pytest.mark.asyncio
async def test_positions_only_app_owned_with_avg_price(monkeypatch):
    def handler(req):
        p = req.url.path
        if p == "/v2/positions":
            return httpx.Response(200, json=[
                {"symbol": "AAPL", "qty": "60", "avg_entry_price": "190.0", "asset_class": "us_equity"},
                {"symbol": "TSLA", "qty": "3", "avg_entry_price": "200", "asset_class": "us_equity"}])
        if p == "/v2/orders":
            return httpx.Response(200, json=[
                {"id": "1", "client_order_id": "tsys-a", "symbol": "AAPL", "side": "buy",
                 "filled_qty": "10", "legs": None}])
        return httpx.Response(200, json=ACCOUNT)
    _mock(monkeypatch, handler)
    ps = await acct.positions()
    assert ps == [acct.NormalizedPosition(symbol="AAPL", quantity=10, quantity_direction="Long",
                                          instrument_type="Equity", avg_price=190.0,
                                          account="••••5678")]


@pytest.mark.asyncio
async def test_option_position_type(monkeypatch):
    def handler(req):
        p = req.url.path
        if p == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "QQQ261117C00660000", "qty": "-2",
                                              "side": "short", "avg_entry_price": "1.5",
                                              "asset_class": "us_option"}])
        if p == "/v2/orders":
            return httpx.Response(200, json=[
                {"id": "1", "client_order_id": "tsys-a", "symbol": "QQQ261117C00660000",
                 "side": "sell", "filled_qty": "2", "legs": None}])
        return httpx.Response(200, json=ACCOUNT)
    _mock(monkeypatch, handler)
    (p,) = await acct.positions()
    assert (p.quantity, p.quantity_direction, p.instrument_type, p.avg_price) == \
        (-2, "Short", "Equity Option", 1.5)


@pytest.mark.asyncio
async def test_cancel_order_refuses_untagged(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=[{"id": "o-manual", "client_order_id": "manual"}])
    _mock(monkeypatch, handler)
    assert await acct.cancel_order("o-manual") is False
    assert "DELETE" not in calls


@pytest.mark.asyncio
async def test_cancel_order_deletes_owned(monkeypatch):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=[{"id": "o1", "client_order_id": "tsys-x", "status": "new"}])
    _mock(monkeypatch, handler)
    assert await acct.cancel_order("o1") is True
    assert ("DELETE", "/v2/orders/o1") in calls


@pytest.mark.asyncio
async def test_cancel_order_blocked_off_paper(monkeypatch):
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=[{"id": "o1", "client_order_id": "tsys-x", "status": "new"}])
    _mock(monkeypatch, handler)
    assert await acct.cancel_order("o1") is False
    assert "DELETE" not in calls


@pytest.mark.asyncio
async def test_reads_degrade_on_error(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(500, json={"message": "boom"}))
    assert await acct.balances() == {}
    assert await acct.positions() == []
    assert await acct.live_orders() == []


@pytest.mark.asyncio
async def test_reads_degrade_on_transport_error(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("down")
    _mock(monkeypatch, handler)
    assert await acct.balances() == {}
    assert await acct.live_orders() == []


@pytest.mark.asyncio
async def test_positions_survive_account_failure(monkeypatch):
    def handler(req):
        p = req.url.path
        if p == "/v2/account":
            return httpx.Response(500, json={"message": "boom"})
        if p == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "AAPL", "qty": "10",
                                              "avg_entry_price": "190", "asset_class": "us_equity"}])
        return httpx.Response(200, json=[{"id": "1", "client_order_id": "tsys-a",
                                          "symbol": "AAPL", "side": "buy",
                                          "filled_qty": "10", "legs": None}])
    _mock(monkeypatch, handler)
    (p,) = await acct.positions()
    assert (p.symbol, p.quantity, p.account) == ("AAPL", 10, "")


@pytest.mark.asyncio
async def test_cancel_nested_child_of_tagged_bracket(monkeypatch):
    calls = []
    parent = {"id": "p1", "client_order_id": "tsys-b", "status": "filled", "symbol": "AAPL",
              "legs": [{"id": "s1", "client_order_id": "gen", "status": "held"}]}

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=[parent])
    _mock(monkeypatch, handler)
    assert await acct.cancel_order("s1") is True
    assert ("DELETE", "/v2/orders/s1") in calls


def _drift_handler(ledger_orders, positions):
    def handler(req):
        p = req.url.path
        if p == "/v2/positions":
            return httpx.Response(200, json=positions)
        if p == "/v2/orders":
            return httpx.Response(200, json=ledger_orders)
        return httpx.Response(200, json=ACCOUNT)
    return handler


@pytest.mark.asyncio
async def test_positions_clamped_to_account(monkeypatch):
    orders = [{"id": "1", "client_order_id": "tsys-a", "symbol": "AAPL", "side": "buy",
               "filled_qty": "10", "legs": None}]
    _mock(monkeypatch, _drift_handler(orders, [
        {"symbol": "AAPL", "qty": "4", "avg_entry_price": "1", "asset_class": "us_equity"}]))
    (p,) = await acct.positions()
    assert p.quantity == 4


@pytest.mark.asyncio
async def test_position_missing_from_account_omitted_with_warning(monkeypatch, caplog):
    orders = [{"id": "1", "client_order_id": "tsys-a", "symbol": "QQQ261117C00660000",
               "side": "buy", "filled_qty": "1", "legs": None}]
    _mock(monkeypatch, _drift_handler(orders, []))
    with caplog.at_level("WARNING", logger="trading.broker.account"):
        assert await acct.positions() == []
    assert any("QQQ261117C00660000" in r.message for r in caplog.records)
