"""Kill switch on cash-settled index options (XSP): marketable-limit closes, never market
orders; legs only count as closed when Alpaca reports the fill; equity options unchanged.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_kill.db ../.venv/bin/python -m pytest tests/test_xsp_kill_switch.py
"""
from __future__ import annotations

import itertools
import time

import pytest

from app.broker import alpaca_client as ac
from app.broker import ledger, orders

SC, SP = "XSP991231C00600000", "XSP991231P00600000"      # far-future: never "expired"
LC, LP = "XSP991231C00610000", "XSP991231P00590000"
QQQ = "QQQ991231C00500000"


class Broker:
    """Scripted Alpaca: app net positions, quotes, and per-order fill behaviour."""

    def __init__(self, held: dict[str, int], quotes: dict[str, tuple]):
        self.held = dict(held)
        self.quotes = quotes
        self.fill = {}                 # sym -> "all" | "one" | "one_once" | "none"
        self.orders: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.limit_bodies: list[dict] = []
        self.market_bodies: list[dict] = []
        self.filled_once: set = set()

    def _qty_fill(self, sym, qty):
        mode = self.fill.get(sym, "all")
        if mode == "all":
            return qty
        if mode == "one":
            return min(1, qty)
        if mode == "one_once":
            if sym in self.filled_once:
                return 0
            self.filled_once.add(sym)
            return min(1, qty)
        return 0

    async def submit_option_order(self, symbol, side, quantity, *, limit_price,
                                  position_intent, strategy_key="", reason=""):
        self.limit_bodies.append({"symbol": symbol, "side": side, "qty": quantity,
                                  "limit": limit_price, "intent": position_intent})
        oid = f"o{next(self.ids)}"
        n = self._qty_fill(symbol, quantity)
        self.orders[oid] = {"id": oid, "symbol": symbol, "status": "new",
                            "filled_qty": str(n), "_side": side}
        if n:
            sign = 1 if side == "buy" else -1
            self.held[symbol] = self.held.get(symbol, 0) + sign * n
            if self.held[symbol] == 0:
                del self.held[symbol]
            self.orders[oid]["status"] = "filled" if n == quantity else "partially_filled"
        return {"transmitted": True, "order_id": oid, "ref": oid}

    async def request(self, method, path, **kw):
        oid = path.rsplit("/", 1)[-1]
        o = self.orders.get(oid)
        if method == "DELETE":
            if o and o["status"] in ("new", "partially_filled"):
                o["status"] = "canceled"
            return None
        return dict(o) if o else None

    async def transmit(self, body, ref, what):
        self.market_bodies.append(body)
        sym, qty = body["symbol"], int(body["qty"])
        sign = 1 if body["side"] == "buy" else -1
        self.held[sym] = self.held.get(sym, 0) + sign * qty
        if self.held[sym] == 0:
            del self.held[sym]
        return {"transmitted": True, "order_id": "m1", "ref": ref}


@pytest.fixture
def broker(monkeypatch):
    from app.config import get_settings
    st = get_settings()
    monkeypatch.setattr(st, "apca_api_key_id", "k")
    monkeypatch.setattr(st, "apca_api_secret_key", "x")
    monkeypatch.setattr(st, "apca_api_base_url", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(st, "enable_live_orders", True)
    for name, v in (("_IDX_ATTEMPT", 0.05), ("_IDX_GROUP_WAIT", 0.6), ("_IDX_STEP", 0.01),
                    ("_CANCEL_WAIT", 0.05), ("_FLATTEN_WAIT", 0.2), ("_FLATTEN_STEP", 0.01)):
        monkeypatch.setattr(orders, name, v)
    b = Broker({SC: -1, SP: -1, LC: 1, LP: 1},
               {SC: (3.20, 3.30), SP: (2.10, 2.20), LC: (0.40, 0.45), LP: (0.30, 0.35)})

    async def held(**kw):
        return dict(b.held)

    async def acct():
        return {k: {"qty": v, "asset_class": "us_option"} for k, v in b.held.items()}

    async def no_open():
        return 0, [], []

    async def quote(sym):
        q = b.quotes.get(sym)              # None = market data unavailable
        if isinstance(q, list):            # scripted sequence, one per call
            return q.pop(0) if len(q) > 1 else q[0]
        return q

    async def not_safe():
        return False

    async def fake_intent(*a, **k):
        return "ref"
    monkeypatch.setattr(ledger, "app_net_positions", held)
    monkeypatch.setattr(ledger, "account_positions", acct)
    monkeypatch.setattr(orders, "_cancel_app_orders", no_open)
    monkeypatch.setattr(orders, "_index_option_quote", quote)
    monkeypatch.setattr(orders, "submit_option_order", b.submit_option_order)
    monkeypatch.setattr(orders, "_transmit", b.transmit)
    monkeypatch.setattr(orders, "is_safe_mode", not_safe)
    monkeypatch.setattr(ac, "request", b.request)
    import app.engine.order_store as order_store
    monkeypatch.setattr(order_store, "record_intent", fake_intent)
    return b


@pytest.mark.asyncio
async def test_index_legs_close_with_marketable_limits_shorts_first(broker):
    out = await orders.flatten_all()
    assert "Flattened 4" in out and broker.held == {} and broker.market_bodies == []
    sides = [(b["symbol"], b["side"], b["intent"]) for b in broker.limit_bodies]
    assert {s for s, side, _ in sides[:2]} == {SC, SP}               # shorts first
    assert all(i == f"{side}_to_close" for _, side, i in sides)
    sc = next(b for b in broker.limit_bodies if b["symbol"] == SC)
    assert sc["limit"] == pytest.approx(3.35)        # ask 3.30 + 0.05, nickel above $3
    lc = next(b for b in broker.limit_bodies if b["symbol"] == LC)
    assert lc["limit"] == pytest.approx(0.35)        # bid 0.40 - 0.05, penny under $3


@pytest.mark.asyncio
async def test_partial_then_cancelled_reports_the_remainder(broker):
    broker.held[SC] = -2
    broker.fill[SC] = "one_once"                     # 1 of 2 fills, then nothing
    with pytest.raises(RuntimeError) as e:
        await orders.flatten_all()
    msg = str(e.value)
    assert "Flatten incomplete" in msg and f"{SC} x1 not closed" in msg
    assert broker.held[SC] == -1
    assert all(o["status"] in ("filled", "canceled") for o in broker.orders.values())
    # The short is still open, so its protection is held back.
    assert f"{LC} x1 (held: a short leg is still open)" in msg and LC in broker.held


@pytest.mark.asyncio
async def test_unfilled_close_is_incomplete_never_success(broker):
    broker.fill[SP] = "none"
    with pytest.raises(RuntimeError) as e:
        await orders.flatten_all()
    assert f"{SP} x1 not closed" in str(e.value) and SP in broker.held


@pytest.mark.asyncio
async def test_long_wing_with_no_bid_is_left_to_settle(broker):
    broker.quotes[LP] = (0.0, 0.05)
    out = await orders.flatten_all()
    assert "left to settle (no bid)" in out and LP in broker.held
    assert not any(b["symbol"] == LP for b in broker.limit_bodies)


@pytest.mark.asyncio
async def test_short_with_no_ask_is_reported_loudly(broker):
    broker.quotes[SC] = (None, None)
    with pytest.raises(RuntimeError) as e:
        await orders.flatten_all()
    assert "short leg has no ask" in str(e.value)


@pytest.mark.asyncio
async def test_short_skipped_before_closing_holds_every_long(broker, monkeypatch):
    """A short the flatten never attempts (its stop order is still cancelling) must keep
    every index long: selling the wings would leave a naked short."""
    async def lingering_on_sc():
        return 0, [], [{"symbol": SC, "status": "pending_cancel"}]
    monkeypatch.setattr(orders, "_cancel_app_orders", lingering_on_sc)
    with pytest.raises(RuntimeError) as e:
        await orders.flatten_all()
    msg = str(e.value)
    assert f"{SC} x1 (held: resting app order not cleared)" in msg
    assert f"{LC} x1 (held: a short leg is still open)" in msg
    assert f"{LP} x1 (held: a short leg is still open)" in msg
    assert not any(b["side"] == "sell" for b in broker.limit_bodies)   # no wing sold
    assert LC in broker.held and LP in broker.held and SC in broker.held


@pytest.mark.asyncio
async def test_short_with_account_mismatch_holds_every_long(broker, monkeypatch):
    async def acct():
        return {k: {"qty": (0 if k == SP else v), "asset_class": "us_option"}
                for k, v in broker.held.items()}
    monkeypatch.setattr(ledger, "account_positions", acct)
    with pytest.raises(RuntimeError) as e:
        await orders.flatten_all()
    msg = str(e.value)
    assert "account position differs" in msg and f"{LP} x1 (held: a short leg" in msg
    assert not any(b["side"] == "sell" for b in broker.limit_bodies)


@pytest.mark.asyncio
async def test_missing_quote_on_a_long_is_a_failure_not_left_to_settle(broker):
    broker.quotes[LP] = None                         # feed down, not "no bid"
    with pytest.raises(RuntimeError) as e:
        await orders.flatten_all()
    msg = str(e.value)
    assert "no quote" in msg and "left to settle" not in msg and LP in broker.held


@pytest.mark.asyncio
async def test_missing_quote_on_a_short_is_retried_then_closed(broker):
    broker.quotes[SC] = [None, None, (3.20, 3.30)]   # transient miss, then a quote
    out = await orders.flatten_all()
    assert "Flattened 4" in out and broker.held == {}


@pytest.mark.asyncio
async def test_kill_route_keeps_index_legs_open_when_alpaca_unconfigured(monkeypatch):
    from app.routers import dashboard
    monkeypatch.setattr(ac, "configured", lambda: False)
    assert await dashboard._index_legs_still_held() is None


@pytest.mark.asyncio
async def test_equity_option_kill_switch_unchanged_market_close(broker):
    broker.held = {QQQ: -1}
    out = await orders.flatten_all()
    assert "Flattened 1" in out and broker.limit_bodies == []
    (b,) = broker.market_bodies
    assert b["type"] == "market" and b["position_intent"] == "buy_to_close"


@pytest.mark.asyncio
async def test_index_flatten_is_time_bounded(broker):
    for s in (SC, SP, LC, LP):
        broker.fill[s] = "none"
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        await orders.flatten_all()
    # shorts concurrently in one bounded window; longs held back (shorts still open)
    assert time.monotonic() - t0 < orders._IDX_GROUP_WAIT * 2 + 1.0


# --- the kill route end to end ----------------------------------------------------
@pytest.mark.asyncio
async def test_close_open_trades_keeps_still_held_index_legs_open():
    from sqlalchemy import delete, select

    from app.db.base import init_db, session_scope
    from app.db.models import TradeRecord
    from app.engine.worker import close_open_trades
    await init_db()
    async with session_scope() as db:
        await db.execute(delete(TradeRecord))
        for sym, tx in ((SC, True), (LC, True), (QQQ, True), ("AAPL", False)):
            db.add(TradeRecord(strategy_key="k", symbol=sym, side="sell", quantity=1.0,
                               status="open", transmitted=tx))
    assert await close_open_trades(keep_index_symbols={SC}) == 3
    async with session_scope() as db:
        rows = {r.symbol: r.status for r in (await db.execute(select(TradeRecord))).scalars()}
    assert rows == {SC: "open", LC: "closed", QQQ: "closed", "AAPL": "closed"}
    assert await close_open_trades(keep_index_symbols=None) == 0      # unreadable: keep
    async with session_scope() as db:
        rows = {r.symbol: r.status for r in (await db.execute(select(TradeRecord))).scalars()}
    assert rows[SC] == "open"


def test_expected_broker_skips_legs_the_kill_switch_closed():
    from app.strategies.xsp_iron_fly.store import expected_broker
    legs = [{"role": "short_call", "symbol": ".a", "occ": SC, "action": "SELL",
             "trade_id": 1, "status": "open"},
            {"role": "long_call", "symbol": ".b", "occ": LC, "action": "BUY",
             "trade_id": 2, "status": "abandoned"},
            {"role": "short_put", "symbol": ".c", "occ": SP, "action": "SELL",
             "trade_id": 3, "status": "open"}]
    fills = [{"symbol": ".a", "side": "SELL", "qty": 1, "kind": "entry"},
             {"symbol": ".b", "side": "BUY", "qty": 1, "kind": "entry"},
             {"symbol": ".c", "side": "SELL", "qty": 1, "kind": "entry"}]
    sess = {"legs": legs, "fills": fills}
    assert expected_broker(sess) == {SC: -1, SP: -1}                   # abandoned out
    assert expected_broker(sess, {3}) == {SC: -1}                      # closed record out


@pytest.mark.asyncio
async def test_pending_cancel_counts_as_open_and_holds_the_symbol(monkeypatch):
    """A pending_cancel order is still on the book: listed as open, and the kill switch
    holds its symbol instead of closing on top of it."""
    pend = {"id": "p1", "client_order_id": "tsys-p1", "status": "pending_cancel",
            "symbol": SC, "order_class": "simple", "legs": []}

    async def app_orders(status="all"):
        return [pend]
    monkeypatch.setattr(ledger, "app_orders", app_orders)
    orders_open, ids = await ledger.app_open_orders()
    assert [o["id"] for o in orders_open] == ["p1"] and "p1" in ids
    assert [o["id"] for o in await orders._lingering(SC)] == ["p1"]
