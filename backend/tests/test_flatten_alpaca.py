"""Kill-switch flatten on the SHARED Alpaca paper account: only app-owned (`tsys-`) orders
are cancelled and only the app's own quantity is closed (capped at the account position),
never via bulk endpoints; gate OFF transmits nothing; the paper lock refuses before any
request; a close that hasn't filled is reported as not flat. Also pins close_open_trades
(the kill switch's TradeRecord ledger step). All HTTP goes through httpx.MockTransport."""
import inspect
import json

import httpx
import pytest
from sqlalchemy import delete, select

from app.broker import account as acct_mod
from app.broker import alpaca_client as ac
from app.broker import orders as ord_mod
from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import TradeRecord


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "PK")
    monkeypatch.setattr(s, "apca_api_secret_key", "sk")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(s, "enable_live_orders", False)

    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    monkeypatch.setattr(ord_mod, "_CANCEL_STEP", 0.0)
    monkeypatch.setattr(ord_mod, "_CANCEL_WAIT", 0.0)
    monkeypatch.setattr(ord_mod, "_FLATTEN_STEP", 0.0)
    monkeypatch.setattr(ord_mod, "_FLATTEN_WAIT", 0.0)


def _gate_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)


def _filled(oid, coid, sym, side, qty):
    return {"id": oid, "client_order_id": coid, "symbol": sym, "side": side,
            "qty": str(qty), "filled_qty": str(qty), "status": "filled", "legs": None}


def _resting(oid, coid, sym):
    return {"id": oid, "client_order_id": coid, "symbol": sym, "side": "sell",
            "qty": "1", "filled_qty": "0", "status": "new", "legs": None}


def _pos(sym, qty, asset_class="us_equity"):
    return {"symbol": sym, "qty": str(abs(qty)), "side": "long" if qty > 0 else "short",
            "avg_entry_price": "100", "asset_class": asset_class}


class Broker:
    """Fake shared Alpaca account. GET /v2/orders serves `orders`, GET /v2/positions
    serves `positions`; POST /v2/orders records the body and, with `fill_on_post`, adds a
    filled order carrying the posted client_order_id (so the ledger nets out);
    DELETE /v2/orders/{id} cancels that order (or nested leg). Every request is recorded."""

    def __init__(self, orders=None, positions=None, fill_on_post=True):
        self.orders = orders or []
        self.positions = positions or []
        self.fill_on_post = fill_on_post
        self.posts: list[dict] = []
        self.requests: list[httpx.Request] = []

    def writes(self):
        return [(r.method, r.url.path) for r in self.requests if r.method != "GET"]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if req.method == "GET" and path == "/v2/orders":
            return httpx.Response(200, json=self.orders)
        if req.method == "GET" and path == "/v2/positions":
            return httpx.Response(200, json=self.positions)
        if req.method == "POST" and path == "/v2/orders":
            body = json.loads(req.content)
            self.posts.append(body)
            oid = f"alp-{len(self.posts)}"
            if self.fill_on_post:
                self.orders.insert(0, _filled(oid, body["client_order_id"], body["symbol"],
                                              body["side"], body["qty"]))
            return httpx.Response(200, json={"id": oid, "status": "accepted",
                                             "client_order_id": body["client_order_id"]})
        if req.method == "DELETE" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[-1]
            for o in [x for p in self.orders for x in [p, *(p.get("legs") or [])]]:
                if o["id"] == oid and o["status"] != "canceled":
                    o["status"] = "canceled"
                    return httpx.Response(204)
            return httpx.Response(422, json={"message": "order is not cancelable"})
        return httpx.Response(404, json={"message": f"unrouted {req.method} {path}"})


_REAL_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*a, **kw)
    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


def _bomb_handler(req):
    raise AssertionError(f"network touched: {req.method} {req.url}")


@pytest.mark.asyncio
async def test_flatten_closes_only_app_qty_when_manual_shares_share_symbol(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10),
                       _filled("m1", "manual-1", "AAPL", "buy", 50)],
               positions=[_pos("AAPL", 60)])
    _mock(monkeypatch, b)
    msg = await ord_mod.flatten_all()
    assert len(b.posts) == 1
    body = b.posts[0]
    assert body["symbol"] == "AAPL" and body["side"] == "sell" and body["qty"] == "10"
    assert body["type"] == "market" and body["time_in_force"] == "day"
    assert ac.is_app_owned(body["client_order_id"])
    assert "AAPL x10" in msg


@pytest.mark.asyncio
async def test_flatten_caps_close_at_account_position(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    # app ledger says +10 but the account only holds 4 (someone sold 6 by hand)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 4)])
    _mock(monkeypatch, b)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert [p["qty"] for p in b.posts] == ["4"]
    assert "AAPL x6" in str(ei.value)           # the uncloseable remainder is reported


@pytest.mark.asyncio
async def test_flatten_account_position_differs_sends_nothing_for_symbol(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", -5)])
    _mock(monkeypatch, b)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert b.posts == []
    assert "account position differs" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_closes_short_option_with_buy_to_close(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    occ = "QQQ260807C00725000"
    b = Broker(orders=[_filled("o1", "tsys-o1", occ, "sell", 2)],
               positions=[_pos(occ, -3, "us_option")])
    _mock(monkeypatch, b)
    msg = await ord_mod.flatten_all()
    assert b.posts == [{"symbol": occ, "qty": "2", "side": "buy", "type": "market",
                        "time_in_force": "day", "position_intent": "buy_to_close",
                        "client_order_id": b.posts[0]["client_order_id"]}]
    assert ac.is_app_owned(b.posts[0]["client_order_id"])
    assert f"{occ} x2" in msg


@pytest.mark.asyncio
async def test_flatten_closes_short_legs_before_long_legs(monkeypatch):
    """A vertical: buy back the short leg before selling the long one."""
    await init_db()
    _gate_on(monkeypatch)
    long_leg, short_leg = "QQQ260807C00700000", "QQQ260807C00725000"
    b = Broker(orders=[_filled("o1", "tsys-o1", long_leg, "buy", 1),
                       _filled("o2", "tsys-o2", short_leg, "sell", 1)],
               positions=[_pos(long_leg, 1, "us_option"), _pos(short_leg, -1, "us_option")])
    _mock(monkeypatch, b)
    await ord_mod.flatten_all()
    assert [(p["symbol"], p["position_intent"]) for p in b.posts] == [
        (short_leg, "buy_to_close"), (long_leg, "sell_to_close")]


@pytest.mark.asyncio
async def test_flatten_never_calls_bulk_endpoints(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_resting("r1", "tsys-r1", "MSFT"),
                       _resting("r2", "manual-2", "MSFT"),
                       _filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 60), _pos("TSLA", 5)])
    _mock(monkeypatch, b)
    await ord_mod.flatten_all()
    writes = b.writes()
    assert not any(p.startswith("/v2/positions") for _, p in writes)   # no position closes
    assert ("DELETE", "/v2/orders") not in writes                      # no cancel-all
    assert sorted(writes) == [("DELETE", "/v2/orders/r1"), ("POST", "/v2/orders")]


@pytest.mark.asyncio
async def test_flatten_cancels_only_app_orders(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_resting("tsys-1-id", "tsys-1", "MSFT"),
                       _resting("manual-id", "manual", "MSFT")])
    _mock(monkeypatch, b)
    msg = await ord_mod.flatten_all()
    assert b.writes() == [("DELETE", "/v2/orders/tsys-1-id")]
    assert "cancelled 1" in msg


@pytest.mark.asyncio
async def test_flatten_cancels_bracket_legs_without_reporting_oco_sibling(monkeypatch):
    """A filled bracket's two resting legs are OCO: cancelling one cancels the other, so
    the sibling's DELETE 422 must not be reported as a failure once both are gone."""
    await init_db()
    _gate_on(monkeypatch)
    legs = [{"id": "tp", "client_order_id": "gen-tp", "symbol": "AAPL", "side": "sell",
             "type": "limit", "status": "new"},
            {"id": "sl", "client_order_id": "gen-sl", "symbol": "AAPL", "side": "sell",
             "type": "stop", "status": "held"}]
    parent = {**_filled("par", "tsys-par", "AAPL", "buy", 10), "order_class": "bracket",
              "legs": legs}
    b = Broker(orders=[parent], positions=[_pos("AAPL", 10)])

    def oco(req):
        resp = b(req)
        if req.method == "DELETE" and resp.status_code == 204:
            for leg in legs:
                leg["status"] = "canceled"
        return resp
    _mock(monkeypatch, oco)
    msg = await ord_mod.flatten_all()
    assert ("DELETE", "/v2/orders/tp") in b.writes()
    assert [p["qty"] for p in b.posts] == ["10"] and "AAPL x10" in msg


@pytest.mark.asyncio
async def test_flatten_reports_order_still_open_after_cancel(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_resting("stuck", "tsys-stuck", "MSFT")])

    def refuse(req):
        if req.method == "DELETE":
            b.requests.append(req)
            return httpx.Response(500, json={"message": "boom"})
        return b(req)
    _mock(monkeypatch, refuse)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert "stuck" in str(ei.value)


def _refuse_deletes(b):
    def handler(req):
        if req.method == "DELETE":
            b.requests.append(req)
            return httpx.Response(500, json={"message": "boom"})
        return b(req)
    return handler


@pytest.mark.asyncio
async def test_flatten_holds_close_while_app_exit_leg_still_live(monkeypatch):
    """A resting app stop-sell that won't cancel could still fire; closing too would sell
    10 of the user's manual shares on the shared account. Hold the close."""
    await init_db()
    _gate_on(monkeypatch)
    sl = {"id": "sl", "client_order_id": "gen-sl", "symbol": "AAPL", "side": "sell",
          "type": "stop", "qty": "10", "filled_qty": "0", "status": "held"}
    parent = {**_filled("par", "tsys-par", "AAPL", "buy", 10), "order_class": "bracket",
              "legs": [sl]}
    b = Broker(orders=[parent, _filled("m1", "manual-1", "AAPL", "buy", 50)],
               positions=[_pos("AAPL", 60)])
    _mock(monkeypatch, _refuse_deletes(b))
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert b.posts == []
    assert "held" in str(ei.value) and "AAPL" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_holds_all_closes_when_open_order_lookup_fails(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    from app.broker import ledger

    async def boom():
        raise ac.AlpacaError(500, "orders down")
    monkeypatch.setattr(ledger, "app_open_orders", boom)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 60)])
    _mock(monkeypatch, b)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert b.posts == []
    assert "held" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_holds_all_closes_when_post_cancel_check_fails(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)

    async def boom(symbol=None):
        raise ac.AlpacaError(500, "orders down")
    monkeypatch.setattr(ord_mod, "_lingering", boom)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 60)])
    _mock(monkeypatch, b)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert b.posts == []
    assert "held" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_lingering_order_does_not_block_unrelated_symbol(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_resting("stuck", "tsys-stuck", "MSFT"),
                       _filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 10)])
    _mock(monkeypatch, _refuse_deletes(b))
    with pytest.raises(RuntimeError) as ei:              # MSFT order still open
        await ord_mod.flatten_all()
    assert [(p["symbol"], p["qty"]) for p in b.posts] == [("AAPL", "10")]
    assert "stuck" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_gate_off_with_app_positions_raises_and_sends_nothing(monkeypatch):
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10),
                       _resting("r1", "tsys-r1", "MSFT")],
               positions=[_pos("AAPL", 10)])
    _mock(monkeypatch, b)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert "AAPL" in str(ei.value)
    assert b.writes() == []


@pytest.mark.asyncio
async def test_flatten_gate_off_no_app_positions(monkeypatch):
    b = Broker(orders=[_filled("m1", "manual-1", "AAPL", "buy", 50)],
               positions=[_pos("AAPL", 50)])
    _mock(monkeypatch, b)
    assert await ord_mod.flatten_all() == "No open positions."
    assert b.writes() == []


@pytest.mark.asyncio
async def test_flatten_broker_read_failure_is_not_reported_flat(monkeypatch):
    """Alpaca configured but unreadable: never claim flat (kill switch fully_flat=False)."""
    _mock(monkeypatch, lambda req: httpx.Response(500, json={"message": "down"}))
    with pytest.raises(RuntimeError) as off:                # gate OFF
        await ord_mod.flatten_all()
    assert "could not read the Alpaca broker" in str(off.value)
    _gate_on(monkeypatch)
    with pytest.raises(RuntimeError) as on:                 # gate ON
        await ord_mod.flatten_all()
    assert "could not read the Alpaca broker" in str(on.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", [False, True])
async def test_flatten_alpaca_not_configured_is_flat_without_requests(monkeypatch, gate):
    """No Alpaca keys -> the app can never have transmitted -> nothing to flatten."""
    monkeypatch.setattr(get_settings(), "apca_api_key_id", "")
    monkeypatch.setattr(get_settings(), "apca_api_secret_key", "")
    monkeypatch.setattr(get_settings(), "enable_live_orders", gate)
    _mock(monkeypatch, _bomb_handler)
    assert await ord_mod.flatten_all() == "No open positions."


@pytest.mark.asyncio
async def test_flatten_reports_not_flat_when_fill_pending(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 10)], fill_on_post=False)
    _mock(monkeypatch, b)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert len(b.posts) == 1
    assert "queued" in str(ei.value) and "AAPL x10" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_paper_lock(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    _mock(monkeypatch, _bomb_handler)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert "paper" in str(ei.value)


@pytest.mark.asyncio
async def test_flatten_transmits_despite_safe_mode(monkeypatch):
    """SAFE_MODE blocks new risk; it must never stop the kill switch closing exposure."""
    await init_db()
    _gate_on(monkeypatch)

    async def safe():
        return True
    monkeypatch.setattr(ord_mod, "is_safe_mode", safe)
    b = Broker(orders=[_filled("e1", "tsys-e1", "AAPL", "buy", 10)],
               positions=[_pos("AAPL", 10)])
    _mock(monkeypatch, b)
    assert "AAPL x10" in await ord_mod.flatten_all()
    assert len(b.posts) == 1


def test_orders_module_has_no_tastytrade_import():
    assert "tastytrade" not in inspect.getsource(ord_mod)
    assert "tastytrade" not in inspect.getsource(acct_mod)


@pytest.mark.asyncio
async def test_close_open_trades_flattens_trade_ledger():
    """Kill switch step 2: every open TradeRecord is closed (dashboard goes flat) with no
    fabricated P&L; idempotent."""
    from app.engine.worker import close_open_trades
    from app.routers.ws import _strategy_positions_snapshot

    await init_db()
    async with session_scope() as s:
        await s.execute(delete(TradeRecord))
        s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                          quantity=10, entry_price=100.0, status="open"))
        s.add(TradeRecord(strategy_key="strategy_1", symbol="MSFT", side="sell",
                          quantity=5, entry_price=200.0, status="open"))
    assert len(await _strategy_positions_snapshot()) == 2
    assert await close_open_trades() == 2
    assert await _strategy_positions_snapshot() == []
    async with session_scope() as s:
        rows = (await s.execute(select(TradeRecord))).scalars().all()
    assert rows and all(r.status == "closed" and r.exit_time is not None for r in rows)
    assert all(r.pnl is None and r.exit_price is None for r in rows)
    assert await close_open_trades() == 0
