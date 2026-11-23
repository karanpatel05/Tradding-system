"""The Alpaca paper adapter for the QQQ vertical: the sign convention (where money is
lost), leg intents, app ownership tag, and refusals that must never POST."""
from __future__ import annotations

import pytest

from app.broker import alpaca_client as ac
from app.strategies.qqq_credit_spread import broker as B

S, L = "QQQ261123P00650000", "QQQ261123P00649000"


def test_opening_limit_is_negative_credit_and_close_is_positive_debit():
    o = B.spread_body(S, L, 1, 0.25, opening=True)
    c = B.spread_body(S, L, 1, 0.07, opening=False)
    assert o["limit_price"] == "-0.25" and c["limit_price"] == "0.07"
    assert B.spread_body(S, L, 1, -0.25, opening=True)["limit_price"] == "-0.25"   # sign-proof
    assert o["order_class"] == "mleg" and o["type"] == "limit" and o["time_in_force"] == "day"
    assert [(x["symbol"], x["side"], x["position_intent"]) for x in o["legs"]] == [
        (S, "sell", "sell_to_open"), (L, "buy", "buy_to_open")]
    assert [(x["symbol"], x["side"], x["position_intent"]) for x in c["legs"]] == [
        (S, "buy", "buy_to_close"), (L, "sell", "sell_to_close")]


def test_fill_parsing():
    o = {"status": "filled", "filled_qty": "1", "filled_avg_price": "-0.41",
         "legs": [{"symbol": S, "filled_avg_price": "0.62"},
                  {"symbol": L, "filled_avg_price": "0.21"}]}
    assert B.net_fill_price(o, True) == 0.41 and B.filled_qty(o) == 1
    assert B.leg_fill_prices(o) == {S: 0.62, L: 0.21}


@pytest.fixture
def routed(monkeypatch):
    from app.config import get_settings
    from app.engine import order_store
    from app.engine import safe_mode
    calls = []

    async def fake_request(method, path, *, params=None, json=None, write=False):
        calls.append((method, path, json))
        return {"id": "alp-1"}

    async def no_safe():
        return False

    async def intent(key, symbol, kind, qty):
        return "ref123"

    async def mark(ref, oid):
        return None
    monkeypatch.setattr(ac, "request", fake_request)
    monkeypatch.setattr(ac, "configured", lambda: True)
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(safe_mode, "is_safe_mode", no_safe)
    monkeypatch.setattr(order_store, "record_intent", intent)
    monkeypatch.setattr(order_store, "mark_perm_id", mark)
    return calls


@pytest.mark.asyncio
async def test_submit_posts_a_tagged_paper_order(routed):
    out = await B.AlpacaSpreadBroker("qqq_credit_spread").submit_spread(S, L, 1, 0.25, True)
    assert out["transmitted"] and out["order_id"] == "alp-1"
    method, path, body = routed[0]
    assert (method, path) == ("POST", "/v2/orders")
    assert body["client_order_id"] == "tsys-ref123" and body["limit_price"] == "-0.25"


@pytest.mark.asyncio
async def test_routing_off_never_posts(routed, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    out = await B.AlpacaSpreadBroker("k").submit_spread(S, L, 1, 0.25, True)
    assert not out["transmitted"] and "order routing is off" in out["detail"]
    assert routed == []


@pytest.mark.asyncio
async def test_safe_mode_never_posts(routed, monkeypatch):
    from app.engine import safe_mode

    async def on():
        return True
    monkeypatch.setattr(safe_mode, "is_safe_mode", on)
    out = await B.AlpacaSpreadBroker("k").submit_spread(S, L, 1, 0.25, True)
    assert out.get("safe_mode") and routed == []


@pytest.mark.asyncio
async def test_live_endpoint_never_posts(routed, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    out = await B.AlpacaSpreadBroker("k").submit_spread(S, L, 1, 0.25, True)
    assert not out["transmitted"] and routed == []


@pytest.mark.asyncio
async def test_cancel_confirmed_waits_through_pending_cancel(monkeypatch):
    seq = iter([{"status": "pending_cancel"}, {"status": "pending_cancel"},
                {"status": "canceled", "filled_qty": "0"}])
    deletes = []

    async def fake_request(method, path, *, params=None, json=None, write=False):
        if method == "DELETE":
            deletes.append(path)
            return None
        return next(seq)
    monkeypatch.setattr(ac, "request", fake_request)
    final = await B.AlpacaSpreadBroker("k").cancel_confirmed("o1", wait=2, step=0.01)
    assert deletes == ["/v2/orders/o1"] and final["status"] == "canceled"


@pytest.mark.asyncio
async def test_leg_quantities_use_the_app_ledger_signs(monkeypatch):
    from app.broker import ledger

    async def net():
        return {S: -1, L: 1, "SPY": 10}
    monkeypatch.setattr(ledger, "app_net_positions", net)
    assert await B.AlpacaSpreadBroker("k").leg_quantities(S, L) == (1, 1)
