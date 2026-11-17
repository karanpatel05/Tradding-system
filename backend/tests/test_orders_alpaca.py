"""Alpaca order routing (paper-locked, shared account): gate/safe-mode/paper-lock ordering,
exact Alpaca request bodies with the `tsys-` ownership tag, durable intent rows, cancel of
app-owned orders only (incl. untagged resting bracket legs), and bracket-fill polling.
All HTTP goes through httpx.MockTransport — never the network."""
import logging
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select

from app.broker import alpaca_client as ac
from app.broker import orders as ord_mod
from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import OrderIntent
from app.engine import execution

BLOCKED = "blocked: Alpaca live endpoint locked (paper only)"


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


def _gate_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)


class Broker:
    """Stateful fake Alpaca: GET /v2/orders serves `orders` (ignores status filter, like
    the ledger tests); POST /v2/orders records the body and returns a new order;
    DELETE /v2/orders/{id} marks that order (or nested leg) canceled."""

    def __init__(self, orders=None, post_status=200, post_json=None):
        self.orders = orders or []
        self.posts: list[dict] = []
        self.deletes: list[str] = []
        self.requests: list[httpx.Request] = []
        self.post_status = post_status
        self.post_json = post_json

    def _find(self, oid):
        for o in self.orders:
            if o.get("id") == oid:
                return o
            for leg in o.get("legs") or []:
                if leg.get("id") == oid:
                    return leg
        return None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        import json
        self.requests.append(req)
        path = req.url.path
        if req.method == "GET" and path == "/v2/orders":
            return httpx.Response(200, json=self.orders)
        if req.method == "POST" and path == "/v2/orders":
            body = json.loads(req.content)
            self.posts.append(body)
            if self.post_status != 200:
                return httpx.Response(self.post_status, json=self.post_json or {})
            return httpx.Response(200, json={"id": f"alp-{len(self.posts)}",
                                             "client_order_id": body.get("client_order_id"),
                                             "status": "accepted"})
        if req.method == "DELETE" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[-1]
            self.deletes.append(oid)
            o = self._find(oid)
            if o is None or o.get("status") == "canceled":
                return httpx.Response(422, json={"message": "order is not cancelable"})
            o["status"] = "canceled"
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unrouted {req.method} {path}"})


_REAL_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*a, **kw)
    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


def _bomb_handler(req):
    raise AssertionError(f"network touched: {req.method} {req.url}")


async def _intent_count(symbol: str) -> int:
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(OrderIntent).where(
            OrderIntent.symbol == symbol))).scalar_one()


async def _intent(ref: str) -> OrderIntent:
    async with session_scope() as s:
        return (await s.execute(select(OrderIntent).where(
            OrderIntent.client_order_ref == ref))).scalars().first()


# ------------------------------------------------------------------ equity
@pytest.mark.asyncio
async def test_equity_gate_off_simulates_without_network(monkeypatch):
    _mock(monkeypatch, _bomb_handler)

    async def mid(symbol):
        return 101.25
    monkeypatch.setattr(ord_mod, "_mid", mid)
    out = await execution.submit_equity_order("AAPL", "enter_long", 10, reason="t")
    assert out["transmitted"] is False and out["paper"] is True
    assert out["fill_price"] == 101.25


@pytest.mark.asyncio
async def test_equity_gate_off_no_mid_no_fill(monkeypatch):
    _mock(monkeypatch, _bomb_handler)

    async def none_mid(symbol):
        return None
    monkeypatch.setattr(ord_mod, "_mid", none_mid)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 10)
    assert out["transmitted"] is False and out.get("fill_price") is None
    assert "no quote" in out["detail"]


@pytest.mark.asyncio
async def test_equity_zero_quantity_and_unknown_intent(monkeypatch):
    _mock(monkeypatch, _bomb_handler)
    out = await ord_mod.submit_equity_order("NVDA", "enter_long", 0)
    assert out["transmitted"] is False and "quantity" in out["detail"]
    with pytest.raises(ValueError):
        await ord_mod.submit_equity_order("NVDA", "bogus", 5)


def test_intent_mapping():
    assert ord_mod.intent_to_action("enter_long") == "BUY"
    assert ord_mod.intent_to_action("exit_short") == "BUY"
    assert ord_mod.intent_to_action("enter_short") == "SELL"
    assert ord_mod.intent_to_action("exit_long") == "SELL"
    with pytest.raises(ValueError):
        ord_mod.intent_to_action("nope")


@pytest.mark.asyncio
async def test_equity_safe_mode_blocks_before_network(monkeypatch):
    _gate_on(monkeypatch)
    _mock(monkeypatch, _bomb_handler)

    async def safe():
        return True
    monkeypatch.setattr(ord_mod, "is_safe_mode", safe)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and out.get("safe_mode") is True


@pytest.mark.asyncio
async def test_equity_paper_lock_blocks_live_host(monkeypatch, caplog):
    await init_db()
    _gate_on(monkeypatch)
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    _mock(monkeypatch, _bomb_handler)
    before = await _intent_count("PLOCK")
    with caplog.at_level(logging.ERROR, logger="trading.broker.orders"):
        out = await ord_mod.submit_equity_order("PLOCK", "enter_long", 5)
    assert out == {"transmitted": False, "detail": BLOCKED}
    assert await _intent_count("PLOCK") == before          # no intent row for a blocked order
    assert any(r.levelno == logging.ERROR and r.name == "trading.broker.orders"
               for r in caplog.records)


@pytest.mark.asyncio
async def test_equity_transmits_tagged_market_order(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker()
    _mock(monkeypatch, b)
    out = await ord_mod.submit_equity_order("AAPL", "exit_short", 5, reason="t")
    assert out["transmitted"] is True and out["order_id"] == "alp-1" and out["ref"]
    assert len(b.posts) == 1
    body = b.posts[0]
    coid = body.pop("client_order_id")
    assert coid.startswith("tsys-") and coid == ac.tag(out["ref"])
    assert body == {"symbol": "AAPL", "qty": "5", "side": "buy", "type": "market",
                    "time_in_force": "day"}
    row = await _intent(out["ref"])
    assert row is not None and row.perm_id == "alp-1" and row.intent == "exit_short"
    from app.engine.order_store import open_refs
    assert out["ref"] in await open_refs()


@pytest.mark.asyncio
async def test_equity_rejection_returns_reason(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker(post_status=422, post_json={"message": "insufficient buying power"})
    _mock(monkeypatch, b)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and not out.get("paper")
    assert "insufficient buying power" in out["detail"]
    assert (await _intent(out["ref"])).perm_id is None


@pytest.mark.asyncio
async def test_equity_transport_error_is_a_failure_not_a_crash(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)

    def boom(req):
        raise httpx.ConnectTimeout("timed out", request=req)
    _mock(monkeypatch, boom)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and not out.get("paper")
    assert out["ref"] in out["detail"]                      # ref logged for reconciliation


# ------------------------------------------------------------------ bracket
@pytest.mark.asyncio
async def test_bracket_gate_off_shape(monkeypatch):
    _mock(monkeypatch, _bomb_handler)
    out = await execution.submit_bracket_order("AAPL", "long", 10, 100.0, 95.0, 110.0)
    assert out["transmitted"] is False and out["paper"] is True and out["bracket"] is True
    assert out["stop"] == 95.0 and out["target"] == 110.0


@pytest.mark.asyncio
async def test_bracket_body_and_shape(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker()
    _mock(monkeypatch, b)
    out = await ord_mod.submit_bracket_order("AAPL", "long", 10, 100.0, 95.0, 110.0)
    assert out["transmitted"] is True and out["bracket"] is True
    assert out["parent_order_id"] == "alp-1" and out["ref"]
    assert out["stop"] == 95.0 and out["target"] == 110.0
    body = b.posts[0]
    assert body.pop("client_order_id") == ac.tag(out["ref"])
    assert body == {"symbol": "AAPL", "qty": "10", "side": "buy", "type": "market",
                    "time_in_force": "gtc", "order_class": "bracket",
                    "take_profit": {"limit_price": "110.0"},
                    "stop_loss": {"stop_price": "95.0"}}
    assert (await _intent(out["ref"])).perm_id == "alp-1"


@pytest.mark.asyncio
async def test_bracket_short_side_and_penny_rounding(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    b = Broker()
    _mock(monkeypatch, b)
    # ATR-derived levels are unrounded floats; Alpaca rejects sub-penny prices.
    out = await ord_mod.submit_bracket_order("AAPL", "short", 3, 100.0,
                                             105.12345, 89.876543)
    assert out["transmitted"] is True
    body = b.posts[0]
    assert body["side"] == "sell"
    assert body["stop_loss"] == {"stop_price": "105.12"}
    assert body["take_profit"] == {"limit_price": "89.88"}
    assert out["stop"] == 105.12345 and out["target"] == 89.876543   # echoed unchanged


@pytest.mark.asyncio
async def test_bracket_paper_lock(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    _mock(monkeypatch, _bomb_handler)
    before = await _intent_count("PLOCKB")
    out = await ord_mod.submit_bracket_order("PLOCKB", "long", 1, 10.0, 9.0, 12.0)
    assert out == {"transmitted": False, "detail": BLOCKED}
    assert await _intent_count("PLOCKB") == before


# ------------------------------------------------------------------ combo
def test_occ_symbol_unpadded():
    assert ord_mod.occ_symbol_for("QQQ", date(2026, 8, 7), 725.0, "C") == "QQQ260807C00725000"
    assert ord_mod.occ_symbol_for("SPY", date(2026, 12, 18), 82.5, "P") == "SPY261218P00082500"
    assert ord_mod.occ_symbol_for("brkb", date(2026, 8, 7), 1000.0, "c") == "BRKB260807C01000000"


LEG_A = {"expiry": date(2026, 8, 7), "strike": 725.0, "right": "C"}
LEG_B = {"expiry": date(2026, 9, 4), "strike": 725.0, "right": "C"}
OCC_A = "QQQ260807C00725000"
OCC_B = "QQQ260904C00725000"


@pytest.mark.asyncio
async def test_combo_gate_off_and_empty(monkeypatch):
    _mock(monkeypatch, _bomb_handler)
    out = await execution.submit_combo_order("QQQ", [{**LEG_A, "action": "SELL"},
                                                     {**LEG_B, "action": "BUY"}],
                                             net_price=1.25, reason="entry")
    assert out["transmitted"] is False and out["paper"] is True
    assert out["legs"] == 2 and "QQQ" in out["detail"]
    assert (await execution.submit_combo_order("QQQ", [], net_price=1.0))["transmitted"] is False


@pytest.mark.asyncio
async def test_combo_rejects_more_than_four_legs(monkeypatch):
    _gate_on(monkeypatch)
    _mock(monkeypatch, _bomb_handler)
    legs = [{"expiry": date(2026, 8, 7), "strike": 700.0 + i, "right": "C", "action": "BUY"}
            for i in range(6)]
    out = await ord_mod.submit_combo_order("QQQ", legs, net_price=1.0)
    assert out["transmitted"] is False and "4" in out["detail"]


@pytest.mark.asyncio
async def test_combo_position_intent_from_app_ledger(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    # The app is short 1 of leg A (its own filled tagged mleg); a manual, untagged order
    # put the shared account long 5 of leg A. Only the app's own net decides open/close.
    app_mleg = {"id": "p1", "client_order_id": "tsys-old", "order_class": "mleg",
                "status": "filled", "symbol": "", "side": "", "legs": [
                    {"id": "l1", "symbol": OCC_A, "side": "sell", "filled_qty": "1",
                     "status": "filled"}]}
    manual = {"id": "m1", "client_order_id": "manual-1", "symbol": OCC_A, "side": "buy",
              "qty": "5", "filled_qty": "5", "status": "filled", "legs": None}
    b = Broker(orders=[app_mleg, manual])
    _mock(monkeypatch, b)
    out = await ord_mod.submit_combo_order("QQQ", [{**LEG_A, "action": "BUY"},
                                                   {**LEG_B, "action": "SELL"}],
                                           net_price=1.85, action="BUY", quantity=3)
    assert out["transmitted"] is True and out["legs"] == 2 and out["order_id"] == "alp-1"
    body = b.posts[0]
    assert body.pop("client_order_id") == ac.tag(out["ref"])
    assert body == {"order_class": "mleg", "qty": "3", "type": "limit",
                    "limit_price": "1.85", "time_in_force": "day", "legs": [
                        {"symbol": OCC_A, "ratio_qty": "1", "side": "buy",
                         "position_intent": "buy_to_close"},
                        {"symbol": OCC_B, "ratio_qty": "1", "side": "sell",
                         "position_intent": "sell_to_open"}]}
    row = await _intent(out["ref"])
    assert row.symbol == "QQQ" and row.perm_id == "alp-1"


@pytest.mark.asyncio
async def test_combo_sell_to_close_and_market_without_net(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)
    app_long = {"id": "p2", "client_order_id": "tsys-old2", "order_class": "simple",
                "status": "filled", "symbol": OCC_A, "side": "buy", "filled_qty": "2",
                "legs": None}
    b = Broker(orders=[app_long])
    _mock(monkeypatch, b)
    out = await ord_mod.submit_combo_order("QQQ", [{**LEG_A, "action": "SELL"},
                                                   {**LEG_B, "action": "BUY"}],
                                           action="SELL")
    assert out["transmitted"] is True
    body = b.posts[0]
    assert body["type"] == "market" and "limit_price" not in body and body["qty"] == "1"
    assert [l["position_intent"] for l in body["legs"]] == ["sell_to_close", "buy_to_open"]


@pytest.mark.asyncio
async def test_combo_limit_price_sign_debit_vs_credit(monkeypatch):
    """Alpaca mleg: positive limit = debit (BUY), negative = credit (SELL)."""
    await init_db()
    _gate_on(monkeypatch)
    b = Broker()
    _mock(monkeypatch, b)
    legs = [{**LEG_A, "action": "SELL"}, {**LEG_B, "action": "BUY"}]
    assert (await ord_mod.submit_combo_order("QQQ", legs, net_price=-1.85,
                                             action="BUY"))["transmitted"]
    assert (await ord_mod.submit_combo_order("QQQ", legs, net_price=2.1,
                                             action="SELL"))["transmitted"]
    assert [p["limit_price"] for p in b.posts] == ["1.85", "-2.1"]


# ------------------------------------------------------------------ cancel
def _open(oid, coid, sym, **kw):
    return {"id": oid, "client_order_id": coid, "symbol": sym, "side": "sell",
            "qty": "1", "filled_qty": "0", "status": "new", "legs": None, **kw}


@pytest.mark.asyncio
async def test_cancel_gate_off_is_noop(monkeypatch):
    _mock(monkeypatch, _bomb_handler)
    assert await execution.cancel_symbol_orders("NVDA") == {"cancelled": 0, "paper": True}


@pytest.mark.asyncio
async def test_cancel_symbol_orders_skips_untagged_and_waits(monkeypatch):
    _gate_on(monkeypatch)
    b = Broker(orders=[_open("o1", "tsys-1", "AAPL"), _open("o2", "manual", "AAPL")])
    _mock(monkeypatch, b)
    out = await ord_mod.cancel_symbol_orders("AAPL")
    assert out == {"cancelled": 1}
    assert b.deletes == ["o1"]
    assert b.orders[1]["status"] == "new"                   # manual order untouched


@pytest.mark.asyncio
async def test_cancel_filled_bracket_resting_legs(monkeypatch):
    """The worker's exit path: entry filled, take-profit + stop legs resting with
    Alpaca-generated (untagged) client ids. Both legs must be targeted; once one is
    cancelled the sibling's DELETE may 422 — that must not abort the loop."""
    _gate_on(monkeypatch)
    tp = _open("tp1", "alpaca-gen-1", "AAPL", type="limit")
    sl = _open("sl1", "alpaca-gen-2", "AAPL", type="stop")
    parent = {"id": "par1", "client_order_id": "tsys-br", "symbol": "AAPL", "side": "buy",
              "qty": "1", "filled_qty": "1", "status": "filled", "order_class": "bracket",
              "legs": [tp, sl]}
    b = Broker(orders=[parent])

    def oco(req):
        resp = b(req)
        if req.method == "DELETE" and resp.status_code == 204:   # OCO: sibling dies too
            for leg in parent["legs"]:
                leg["status"] = "canceled"
        return resp
    _mock(monkeypatch, oco)
    out = await ord_mod.cancel_symbol_orders("AAPL")
    assert set(b.deletes) == {"tp1", "sl1"}
    assert out["cancelled"] >= 1 and "pending" not in out


@pytest.mark.asyncio
async def test_cancel_reports_pending_when_orders_linger(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setattr(ord_mod, "_CANCEL_WAIT", 0.05)
    b = Broker(orders=[_open("o1", "tsys-1", "AAPL")])

    def sticky(req):
        if req.method == "DELETE":
            b.deletes.append(req.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)                      # accepted, but stays open
        return b(req)
    _mock(monkeypatch, sticky)
    out = await ord_mod.cancel_symbol_orders("AAPL")
    assert out == {"cancelled": 1, "pending": True}


@pytest.mark.asyncio
async def test_cancel_underlying_matches_option_root_only(monkeypatch):
    _gate_on(monkeypatch)
    b = Broker(orders=[
        _open("q1", "tsys-q1", "QQQ260807C00725000"),
        _open("qm", "tsys-qm", "QQQM260807C00200000"),
        _open("ml", "tsys-ml", "", order_class="mleg", legs=[
            {"id": "ml-a", "symbol": "QQQ260904C00725000", "status": "new"}]),
        _open("aapl", "tsys-a", "AAPL"),
    ])
    _mock(monkeypatch, b)
    out = await ord_mod.cancel_symbol_orders("QQQ")
    assert sorted(b.deletes) == ["ml", "q1"]
    assert out == {"cancelled": 2}


@pytest.mark.asyncio
async def test_cancel_waits_through_pending_cancel(monkeypatch):
    """Alpaca DELETE is async (204 -> pending_cancel -> canceled) and shares stay
    reserved meanwhile, so pending_cancel must count as still open."""
    _gate_on(monkeypatch)
    o1 = _open("o1", "tsys-1", "AAPL")
    b = Broker(orders=[o1])
    seen = []

    def async_cancel(req):
        if req.method == "DELETE":
            b.deletes.append(req.url.path.rsplit("/", 1)[-1])
            o1["status"] = "pending_cancel"
            return httpx.Response(204)
        resp = b(req)
        seen.append(o1["status"])
        if o1["status"] == "pending_cancel":
            o1["status"] = "canceled"                      # settles after one poll
        return resp
    _mock(monkeypatch, async_cancel)
    out = await ord_mod.cancel_symbol_orders("AAPL")
    assert out == {"cancelled": 1}
    assert seen.count("pending_cancel") == 1 and seen[-1] == "canceled"   # it waited


@pytest.mark.asyncio
async def test_cancel_stuck_in_pending_cancel_reports_pending(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setattr(ord_mod, "_CANCEL_WAIT", 0.05)
    o1 = _open("o1", "tsys-1", "AAPL")
    b = Broker(orders=[o1])

    def stuck(req):
        if req.method == "DELETE":
            o1["status"] = "pending_cancel"
            return httpx.Response(204)
        return b(req)
    _mock(monkeypatch, stuck)
    assert await ord_mod.cancel_symbol_orders("AAPL") == {"cancelled": 1, "pending": True}


# ------------------------------------------------------------------ poll
DEAD = {"price": None, "ms": None, "reason": "bracket"}

def _bracket(pid, coid, sym, legs, status="filled"):
    return {"id": pid, "client_order_id": coid, "symbol": sym, "side": "buy", "qty": "10",
            "filled_qty": "10", "status": status, "order_class": "bracket", "legs": legs}


def _leg(lid, typ, status="new", price=None, at=None):
    return {"id": lid, "client_order_id": f"gen-{lid}", "symbol": "AAPL", "side": "sell",
            "type": typ, "status": status, "filled_avg_price": price, "filled_at": at}


@pytest.mark.asyncio
async def test_poll_bracket_fill_stop_and_target(monkeypatch):
    _gate_on(monkeypatch)
    at = "2026-11-17T15:30:00.123456789Z"
    want_ms = 1_794_929_400_123
    b = Broker(orders=[_bracket("par1", "tsys-x", "AAPL", [
        _leg("tp", "limit", status="canceled"),
        _leg("sl", "stop", status="filled", price="98.5", at=at)])])
    _mock(monkeypatch, b)
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") == {
        "price": 98.5, "ms": want_ms, "reason": "stop"}

    b.orders = [_bracket("par1", "tsys-x", "AAPL", [
        _leg("tp", "limit", status="filled", price="110.25", at=at),
        _leg("sl", "stop", status="canceled")])]
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") == {
        "price": 110.25, "ms": want_ms, "reason": "target"}

    b.orders = [_bracket("par1", "tsys-x", "AAPL", [_leg("tp", "limit"), _leg("sl", "stop")])]
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") is None


@pytest.mark.asyncio
async def test_poll_bracket_fill_by_ref_then_latest_for_symbol(monkeypatch):
    _gate_on(monkeypatch)
    at = "2026-11-17T15:30:00Z"
    newer = _bracket("new", "tsys-new", "AAPL", [_leg("t2", "limit"), _leg("s2", "stop")])
    older = _bracket("old", "tsys-old", "AAPL", [
        _leg("t1", "limit", status="filled", price="111", at=at), _leg("s1", "stop")])
    untagged = _bracket("man", "manual", "AAPL", [
        _leg("t3", "limit", status="filled", price="1", at=at), _leg("s3", "stop")])
    b = Broker(orders=[untagged, newer, older])            # newest first
    _mock(monkeypatch, b)
    got = await ord_mod.poll_bracket_fill("AAPL", "old")    # by stored parent id
    assert got["reason"] == "target" and got["price"] == 111.0
    assert await ord_mod.poll_bracket_fill("AAPL", "missing") is None   # latest app = newer
    assert await ord_mod.poll_bracket_fill("AAPL", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_status", ["canceled", "rejected", "expired"])
async def test_poll_dead_parent_entry_never_filled(monkeypatch, parent_status):
    _gate_on(monkeypatch)
    par = _bracket("par1", "tsys-x", "AAPL", [_leg("tp", "limit", status="canceled"),
                                              _leg("sl", "stop", status="canceled")],
                   status=parent_status)
    par["filled_qty"] = "0"
    _mock(monkeypatch, Broker(orders=[par]))
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") == DEAD


@pytest.mark.asyncio
async def test_poll_entry_filled_but_all_exits_terminal_unfilled(monkeypatch):
    _gate_on(monkeypatch)
    _mock(monkeypatch, Broker(orders=[_bracket("par1", "tsys-x", "AAPL", [
        _leg("tp", "limit", status="canceled"), _leg("sl", "stop", status="expired")])]))
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") == DEAD


@pytest.mark.asyncio
async def test_poll_no_app_bracket_found(monkeypatch):
    _gate_on(monkeypatch)
    manual = _bracket("man", "manual", "MSFT", [_leg("t", "limit"), _leg("s", "stop")])
    _mock(monkeypatch, Broker(orders=[manual]))
    assert await ord_mod.poll_bracket_fill("MSFT", "gone") == DEAD
    assert await ord_mod.poll_bracket_fill("MSFT", None) == DEAD


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["new", "held", "accepted", "pending_cancel"])
async def test_poll_still_holding_while_an_exit_child_is_live(monkeypatch, live_status):
    _gate_on(monkeypatch)
    _mock(monkeypatch, Broker(orders=[_bracket("par1", "tsys-x", "AAPL", [
        _leg("tp", "limit", status="canceled"), _leg("sl", "stop", status=live_status)])]))
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") is None


@pytest.mark.asyncio
async def test_poll_bracket_fill_gate_off_and_error(monkeypatch):
    _mock(monkeypatch, _bomb_handler)
    assert await ord_mod.poll_bracket_fill("AAPL", "x") is None
    _gate_on(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(500, json={"message": "down"}))
    assert await ord_mod.poll_bracket_fill("AAPL", "x") == {
        "price": None, "ms": None, "reason": "error"}


# ------------------------------------------------------------------ no Tastytrade
@pytest.mark.asyncio
async def test_submit_paths_never_touch_tastytrade(monkeypatch):
    await init_db()
    _gate_on(monkeypatch)

    async def tt_bomb(*a, **k):
        raise AssertionError("Tastytrade touched on an Alpaca order path")
    for name in ("_session", "_account", "_equities", "_option"):
        monkeypatch.setattr(ord_mod, name, tt_bomb)
    b = Broker(orders=[_open("o1", "tsys-1", "AAPL"),
                       _bracket("par1", "tsys-x", "AAPL", [_leg("tp", "limit"),
                                                           _leg("sl", "stop")])])
    _mock(monkeypatch, b)
    assert (await ord_mod.submit_equity_order("AAPL", "enter_long", 1))["transmitted"]
    assert (await ord_mod.submit_bracket_order("AAPL", "long", 1, 100, 95, 110))["transmitted"]
    assert (await ord_mod.submit_combo_order("QQQ", [{**LEG_A, "action": "BUY"}],
                                             net_price=1.0))["transmitted"]
    assert (await ord_mod.cancel_symbol_orders("AAPL"))["cancelled"] >= 1
    # its exit legs were just cancelled with none filled -> dead bracket, not "holding"
    assert await ord_mod.poll_bracket_fill("AAPL", "par1") == DEAD
