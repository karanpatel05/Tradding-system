"""XSP iron fly on Alpaca paper (fake Alpaca): real-order executor semantics, the runner
end to end in alpaca_paper mode, restart recovery, EOD reconciliation, the expiry-aware
ledger, and activation gating. No network.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_xspa.db ../.venv/bin/python -m pytest tests/test_xsp_fly_alpaca.py
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.broker import ledger
from app.db.base import init_db, session_scope
from app.db.models import SystemFlag, TradeRecord, XspFlySession
from app.strategies.xsp_iron_fly import logic as L
from app.strategies.xsp_iron_fly import store
from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaFlyExecutor, occ
from tests.test_xsp_fly_sim import D, P, FakeFeed, SimClock, at, flat, make, ramp

ET = ZoneInfo("America/New_York")
KEY = "xsp_iron_fly"
PA = replace(P, mode="alpaca_paper")


class FakeAlpaca:
    """Port double. Orders fill against the feed's quotes when marketable, at the touch
    (combo legs: shorts at the bid, wings at the ask). Knobs: ``reject`` (submit
    refused), ``fill_cap`` (max qty per order), ``open_polls`` (polls an order stays
    open before it can fill), ``fill_on_cancel`` (a resting order fills during the
    cancel), ``unknown`` (submit result lost; the order did land)."""

    def __init__(self, feed) -> None:
        self.feed = feed
        self.orders: dict[str, dict] = {}
        self.bodies: list[dict] = []
        self.ids = itertools.count(1)
        self.reject: str | None = None
        self.fill_cap: int | None = None
        self.open_polls = 0
        self.fill_on_cancel = False
        self.unknown = False
        self.fill_once = False                   # after a partial, nothing more fills
        self.unreadable_after = None             # order ids >= this read back as None
        self.extra: dict[str, int] = {}          # fills Alpaca has that the app didn't see

    def _touch(self, occ_sym):
        for leg in self._legs.values():
            if occ(leg) == occ_sym:
                q = self.feed.quote(leg["symbol"])
                return q.bid, q.ask
        raise KeyError(occ_sym)

    def bind(self, legs):
        self._legs = {l["symbol"]: l for l in legs}

    async def submit(self, body, intent, qty):
        self.bodies.append(body)
        if self.reject:
            return {"transmitted": False, "detail": f"rejected by Alpaca: {self.reject}"}
        oid = f"o{next(self.ids)}"
        self.orders[oid] = {"id": oid, "status": "new", "filled_qty": "0", "body": body,
                            "polls": 0, "client_order_id": f"tsys-{oid}", "legs": []}
        if self.unknown:
            return {"transmitted": False, "unknown_state": True, "ref": oid}
        return {"transmitted": True, "order_id": oid, "ref": oid}

    async def find_by_ref(self, ref):
        return self.orders.get(ref)

    def _try_fill(self, o):
        b = o["body"]
        if self.fill_once and int(o["filled_qty"]):
            return
        want = int(b["qty"]) - int(o["filled_qty"])
        n = min(want, self.fill_cap) if self.fill_cap else want
        if b.get("order_class") == "mleg":
            touches = {x["symbol"]: self._touch(x["symbol"]) for x in b["legs"]}
            nat = (sum(touches[x["symbol"]][0] for x in b["legs"] if x["side"] == "sell")
                   - sum(touches[x["symbol"]][1] for x in b["legs"] if x["side"] == "buy"))
            if nat + 1e-9 < -float(b["limit_price"]):
                return
            o["legs"] = [{"symbol": x["symbol"], "side": x["side"],
                          "filled_qty": str(int(o["filled_qty"]) + n),
                          "filled_avg_price": str(touches[x["symbol"]][0 if x["side"] == "sell"
                                                                       else 1])}
                         for x in b["legs"]]
        else:
            bid, ask = self._touch(b["symbol"])
            lim = float(b["limit_price"])
            if b["side"] == "buy" and lim + 1e-9 < ask or b["side"] == "sell" and lim > bid:
                return
            o["filled_avg_price"] = str(ask if b["side"] == "buy" else bid)
        o["filled_qty"] = str(int(o["filled_qty"]) + n)
        o["status"] = "filled" if int(o["filled_qty"]) >= int(b["qty"]) else "partially_filled"

    async def get_order(self, oid):
        if self.unreadable_after and int(oid[1:]) >= self.unreadable_after:
            return None
        o = self.orders[oid]
        if o["status"] in ("new", "partially_filled"):
            o["polls"] += 1
            if o["polls"] > self.open_polls:
                self._try_fill(o)
        return {k: v for k, v in o.items() if k != "body"}

    async def cancel(self, oid):
        o = self.orders[oid]
        if o["status"] in ("new", "partially_filled"):
            if self.fill_on_cancel:
                cap, self.fill_cap = self.fill_cap, None
                self._try_fill(o)
                self.fill_cap = cap
            if o["status"] != "filled":
                o["status"] = "canceled"

    async def positions(self, *, include_expired=False):
        net = dict(self.extra)
        for o in self.orders.values():
            b = o["body"]
            if b.get("order_class") == "mleg":
                for x in o["legs"]:
                    s = 1 if x["side"] == "buy" else -1
                    net[x["symbol"]] = net.get(x["symbol"], 0) + s * int(x["filled_qty"])
            elif int(o["filled_qty"]):
                s = 1 if b["side"] == "buy" else -1
                net[b["symbol"]] = net.get(b["symbol"], 0) + s * int(o["filled_qty"])
        return {k: v for k, v in net.items() if v}

    async def open_orders(self):
        return [{"id": o["id"], "symbol": o["body"].get("symbol"),
                 "legs": o["body"].get("legs") or []}
                for o in self.orders.values() if o["status"] in ("new", "partially_filled")]


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with session_scope() as s:
        await s.execute(delete(XspFlySession))
        await s.execute(delete(TradeRecord).where(TradeRecord.strategy_key == KEY))
        await s.execute(delete(SystemFlag).where(SystemFlag.key.in_((store.HALT_FLAG,
                                                                     "safe_mode"))))
    yield
    async with session_scope() as s:
        await s.execute(delete(SystemFlag).where(SystemFlag.key == "safe_mode"))


async def _legs(clock, feed):
    """The day's four legs as the runner builds them (symbols bound to the fake)."""
    cs = await feed.contracts("XSP", D)
    by = {(c.right, c.strike): c for c in cs}
    legs = L.build_fly(600.0, 610.0, 590.0)
    for l in legs:
        c = by[(l["right"], l["strike"])]
        l.update(symbol=c.symbol, occ=c.occ)
    return legs


def ex_for(clock, feed, alp, **kw):
    return AlpacaFlyExecutor(alp, feed, clock, window=2.0, step=0.5, cancel_wait=2.0, **kw)


# --- executor -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_combo_sends_negative_credit_limit_and_returns_real_leg_fills():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    q = {l["role"]: feed.quote(l["symbol"]) for l in legs}
    nat = q["short_call"].bid + q["short_put"].bid - q["long_call"].ask - q["long_put"].ask
    res = await ex_for(clock, feed, alp).combo(legs, 1, nat, max_age=5)
    body = alp.bodies[0]
    assert body["order_class"] == "mleg" and body["time_in_force"] == "day"
    assert float(body["limit_price"]) == pytest.approx(-round(nat, 2))   # negative = credit
    assert [(x["side"], x["position_intent"]) for x in body["legs"]] == [
        ("sell", "sell_to_open"), ("sell", "sell_to_open"), ("buy", "buy_to_open"),
        ("buy", "buy_to_open")]
    assert res.status == "filled" and res.filled_qty == 1
    assert res.prices["short_call"] == pytest.approx(q["short_call"].bid)
    assert res.prices["long_put"] == pytest.approx(q["long_put"].ask)


@pytest.mark.asyncio
async def test_combo_partial_cancels_the_rest_and_reports_filled_qty():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    alp.fill_cap, alp.fill_once = 1, True
    res = await ex_for(clock, feed, alp).combo(legs, 3, 0.5, max_age=5)
    # 1 of 3 fills, the window ends, the rest is cancelled
    assert res.status == "partial" and res.filled_qty == 1
    o = alp.orders["o1"]
    assert o["status"] == "canceled" and int(o["filled_qty"]) == res.filled_qty


@pytest.mark.asyncio
async def test_rejected_order_is_reported_and_nothing_assumed():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    alp.reject = "index options are not enabled"
    res = await ex_for(clock, feed, alp).combo(legs, 1, 1.0, max_age=5)
    assert res.status == "rejected" and res.filled_qty == 0
    assert "index options are not enabled" in res.detail


@pytest.mark.asyncio
async def test_unknown_submit_state_is_resolved_by_client_order_id():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    alp.unknown = True
    res = await ex_for(clock, feed, alp).combo(legs, 1, 0.5, max_age=5)
    assert res.status == "filled"          # found the landed order, read its real fill


@pytest.mark.asyncio
async def test_fill_that_lands_during_the_cancel_is_counted():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    alp.open_polls = 10**6                  # never fills while polled
    alp.fill_on_cancel = True
    res = await ex_for(clock, feed, alp).combo(legs, 1, 0.5, max_age=5)
    assert res.status == "filled" and alp.orders["o1"]["status"] == "filled"


@pytest.mark.asyncio
async def test_stop_close_is_single_leg_buy_to_close_on_a_nickel():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    sc = legs[0]
    q = feed.quote(sc["symbol"])
    res = await ex_for(clock, feed, alp).single(sc, "BUY", 1, q.ask + 0.03, max_age=30)
    b = alp.bodies[0]
    assert (b["side"], b["position_intent"], b["type"], b["time_in_force"]) == (
        "buy", "buy_to_close", "limit", "day")
    assert round(float(b["limit_price"]) * 100) % 5 == 0          # nickel, rounded up
    assert float(b["limit_price"]) >= q.ask + 0.03 - 1e-9
    assert res.status == "filled" and res.prices["short_call"] == pytest.approx(q.ask)


@pytest.mark.asyncio
async def test_stale_quote_blocks_submission():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    feed.overrides[legs[0]["symbol"]] = lambda t: (2.0, 2.1, t - timedelta(seconds=60))
    res = await ex_for(clock, feed, alp).combo(legs, 1, 0.5, max_age=5)
    assert res.status == "unfilled" and "stale" in res.detail and alp.bodies == []


# --- runner end to end ------------------------------------------------------------
def _runner(clock, feed, alp, params=PA, **kw):
    ex = ex_for(clock, feed, alp)
    return make(clock, feed, params, executor=ex, **kw)


async def _bound_fake(clock, feed):
    alp = FakeAlpaca(feed)
    alp.bind(await _legs(clock, feed))
    return alp


@pytest.mark.asyncio
async def test_day_on_alpaca_entry_stop_settlement_and_eod_reconciliation():
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    feed = FakeFeed(clock, path)
    alp = await _bound_fake(clock, feed)
    s = await _runner(clock, feed, alp).run_session(D)
    assert s["status"] == "settled" and s["mode"] == "alpaca_paper"
    sc = next(l for l in s["legs"] if l["role"] == "short_call")
    assert sc["status"] == "stopped" and sc["exit_price"] >= sc["stop_level"] - 1e-9
    kinds = [b.get("order_class") or b["position_intent"] for b in alp.bodies]
    assert kinds[-1] == "buy_to_close" and kinds.count("buy_to_close") == 1  # no wing stops
    rec = s["reconciliation"]
    assert rec["ok"] and rec["broker"]["issues"] == [], rec
    async with session_scope() as db:
        trs = (await db.execute(select(TradeRecord).where(
            TradeRecord.strategy_key == KEY))).scalars().all()
    assert len(trs) == 4 and all(t.transmitted and " " not in t.symbol for t in trs)
    assert {t.symbol for t in trs} == {occ(l) for l in s["legs"]}


@pytest.mark.asyncio
async def test_eod_reconciliation_flags_a_fill_the_session_never_saw():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    runner = _runner(clock, feed, alp)
    task = asyncio.ensure_future(runner.run_session(D))
    while clock.now() < at(D, 12, 0):
        await asyncio.sleep(0)
    alp.extra[occ((await _legs(clock, feed))[2])] = 1     # a stray long-call fill
    s = await task
    assert s["status"] == "settled" and not s["reconciliation"]["ok"]
    assert any("Alpaca +2 vs session +1" in i for i in s["reconciliation"]["broker"]["issues"])


async def _crash_at(coro, clock, when):
    task = asyncio.ensure_future(coro)
    while clock.now() < when and not task.done():
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_restart_resumes_when_alpaca_matches_and_trips_safe_mode_when_not():
    from app.engine.safe_mode import is_safe_mode
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    await _crash_at(_runner(clock, feed, alp).run_session(D), clock, at(D, 11, 0))
    assert (await store.get_session(KEY, D))["status"] == "open"
    # Clean restart: same fake Alpaca, fresh runner -> resumes and settles.
    feed2 = FakeFeed(clock, flat(600.0))
    await feed2.contracts("XSP", D)
    s = await _runner(clock, feed2, alp).run_session(D)
    assert s["status"] == "settled" and not await is_safe_mode()


@pytest.mark.asyncio
async def test_restart_with_an_unrecorded_alpaca_fill_trips_safe_mode():
    from app.engine.safe_mode import is_safe_mode
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    await _crash_at(_runner(clock, feed, alp).run_session(D), clock, at(D, 11, 0))
    legs = await _legs(clock, feed)
    alp.extra[occ(legs[0])] = 1             # e.g. a stop fill that landed after the crash
    feed2 = FakeFeed(clock, flat(600.0))
    await feed2.contracts("XSP", D)
    s = await _runner(clock, feed2, alp).run_session(D)
    assert s["status"] == "error" and "Alpaca positions differ" in s["reason"]
    assert await is_safe_mode()


@pytest.mark.asyncio
async def test_restart_cancels_orders_a_crash_left_working():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    await _crash_at(_runner(clock, feed, alp).run_session(D), clock, at(D, 11, 0))
    legs = await _legs(clock, feed)
    await alp.submit({"symbol": occ(legs[0]), "qty": "1", "side": "buy", "type": "limit",
                      "limit_price": "0.05", "time_in_force": "day",
                      "position_intent": "buy_to_close"}, "leg", 1)
    alp.open_polls = 10**6
    feed2 = FakeFeed(clock, flat(600.0))
    await feed2.contracts("XSP", D)
    task = asyncio.ensure_future(_runner(clock, feed2, alp).run_session(D))
    while clock.now() < at(D, 11, 5):
        await asyncio.sleep(0)
    assert alp.orders["o2"]["status"] == "canceled"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_unreadable_stop_close_trips_safe_mode_and_stops_managing():
    from app.engine.safe_mode import is_safe_mode
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    feed = FakeFeed(clock, path)
    alp = await _bound_fake(clock, feed)
    alp.unreadable_after = 2                 # the entry (o1) reads fine; the stop does not
    s = await _runner(clock, feed, alp).run_session(D)
    assert s["status"] == "error" and "state unknown" in s["reason"]
    assert await is_safe_mode()
    assert s["settlement_price"] is None     # left for a human, not settled over


@pytest.mark.asyncio
async def test_rejected_entry_skips_the_day_with_nothing_open():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    alp.reject = "options trading level too low"
    s = await _runner(clock, feed, alp).run_session(D)
    assert s["status"] == "skipped" and "options trading level too low" in s["reason"]
    assert await alp.positions() == {}


@pytest.mark.asyncio
async def test_sim_opened_session_is_not_managed_in_alpaca_mode():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    await _crash_at(make(clock, feed).run_session(D), clock, at(D, 11, 0))   # sim mode
    feed2 = FakeFeed(clock, flat(600.0))
    await feed2.contracts("XSP", D)
    alp = await _bound_fake(clock, feed2)
    s = await _runner(clock, feed2, alp).run_session(D)
    assert s["status"] == "mode_mismatch" and alp.bodies == []
    assert (await store.get_session(KEY, D))["status"] == "open"           # untouched


# --- ledger expiry --------------------------------------------------------------
def test_option_expired_rule():
    t = lambda d, hh, mm: datetime.combine(d, time(hh, mm), tzinfo=ET)  # noqa: E731
    sym = "XSP261123C00600000"
    assert not ledger.option_expired(sym, t(D, 15, 59))
    assert ledger.option_expired(sym, t(D, 16, 0))
    assert ledger.option_expired(sym, t(D + timedelta(days=1), 9, 0))
    assert not ledger.option_expired("SPY", t(D + timedelta(days=9), 9, 0))
    assert not ledger.option_expired("QQQ261124P00590000", t(D, 16, 30))


@pytest.mark.asyncio
async def test_app_net_positions_drops_expired_options(monkeypatch):
    orders = [{"order_class": "mleg", "client_order_id": "tsys-a", "legs": [
        {"symbol": "XSP261123C00600000", "side": "sell", "filled_qty": "1"},
        {"symbol": "XSP261123C00610000", "side": "buy", "filled_qty": "1"}]},
        {"client_order_id": "tsys-b", "symbol": "SPY", "side": "buy", "filled_qty": "5"}]

    async def fake_orders(status="all"):
        return orders
    monkeypatch.setattr(ledger, "app_orders", fake_orders)
    before = datetime.combine(D, time(15, 0), tzinfo=ET)
    after = datetime.combine(D, time(16, 5), tzinfo=ET)
    assert await ledger.app_net_positions(now=before) == {
        "XSP261123C00600000": -1, "XSP261123C00610000": 1, "SPY": 5}
    assert await ledger.app_net_positions(now=after) == {"SPY": 5}
    assert len(await ledger.app_net_positions(now=after, include_expired=True)) == 3


# --- activation gating ------------------------------------------------------------
def test_alpaca_mode_activation_requires_order_routing(monkeypatch):
    from app.config import get_settings
    from app.strategies.xsp_iron_fly import XspIronFlyStrategy
    from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaFlyExecutor as AFE
    from app.strategies.xsp_iron_fly.params import IronFlyParams
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "k")
    monkeypatch.setattr(s, "apca_api_secret_key", "s")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")
    strat = XspIronFlyStrategy(IronFlyParams(mode="alpaca_paper"))
    monkeypatch.setattr(s, "enable_live_orders", False)
    assert "ENABLE_LIVE_ORDERS" in strat.activation_block_reason()
    monkeypatch.setattr(s, "enable_live_orders", True)
    assert strat.activation_block_reason() is None
    assert isinstance(strat.build_runner().ex, AFE)
    assert strat.execution_label() == "Alpaca paper"
    monkeypatch.setattr(s, "apca_api_base_url", "https://api.alpaca.markets")
    assert "paper lock" in strat.activation_block_reason()


# --- review fixes ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_that_does_not_take_is_unknown_not_unfilled():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.bind(legs)
    alp.open_polls = 10**6                   # never fills while polled

    async def stuck_cancel(oid):             # the cancel is ignored: still working
        return None
    alp.cancel = stuck_cancel
    ex = ex_for(clock, feed, alp)
    res = await ex.combo(legs, 1, 0.5, max_age=5)
    assert res.status == "unknown" and "after cancel" in res.detail
    res = await ex.single(legs[0], "BUY", 1, 5.0, max_age=30)
    assert res.status == "unknown"


@pytest.mark.asyncio
async def test_rejected_stop_close_alerts_once_and_keeps_retrying():
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    feed = FakeFeed(clock, path)
    alp = await _bound_fake(clock, feed)
    real_submit = alp.submit

    async def reject_stops(body, intent, qty):
        if body.get("position_intent") == "buy_to_close":
            return {"transmitted": False, "detail": "rejected by Alpaca: test"}
        return await real_submit(body, intent, qty)
    alp.submit = reject_stops
    alerts = []
    s = await _runner(clock, feed, alp, alerts=alerts).run_session(D)
    rejected = [k for k, _ in alerts if k == "xsp_fly_close_rejected"]
    assert len(rejected) == 1                # alerted once, not every tick
    sc = next(l for l in s["legs"] if l["role"] == "short_call")
    assert sc["exit_reason"] == "settlement"  # never closed: settled at the close


@pytest.mark.asyncio
async def test_boot_reconciliation_with_real_occ_legs_before_and_after_the_close(monkeypatch):
    """The global startup recovery (not just the runner's own check) with real XSP OCC
    symbols: clean while the legs are open, and still clean at 16:05 before the runner
    has booked the settlement (both sides drop expired contracts)."""
    from datetime import datetime as _dt

    from app.config import get_settings
    from app.engine import recovery
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "k")
    monkeypatch.setattr(s, "apca_api_secret_key", "x")
    monkeypatch.setattr(recovery, "_schedule_backfill", lambda: None)
    syms = {"XSP261123C00600000": ("sell", -1), "XSP261123P00600000": ("sell", -1),
            "XSP261123C00610000": ("buy", 1), "XSP261123P00590000": ("buy", 1)}
    order = {"order_class": "mleg", "client_order_id": "tsys-x", "status": "filled",
             "legs": [{"symbol": k, "side": side, "filled_qty": "1"}
                      for k, (side, _) in syms.items()]}

    async def app_orders(status="all"):
        return [order]

    async def account_positions():
        return {k: {"qty": q, "avg_entry_price": 1.0} for k, (_, q) in syms.items()}
    monkeypatch.setattr(ledger, "app_orders", app_orders)
    monkeypatch.setattr(ledger, "account_positions", account_positions)
    async with session_scope() as db:      # this throwaway DB holds other tests' rows
        await db.execute(delete(TradeRecord).where(TradeRecord.transmitted.is_(True)))
        for k, (side, _) in syms.items():
            db.add(TradeRecord(strategy_key=KEY, symbol=k, side=side, quantity=1.0,
                               entry_price=1.0, status="open", transmitted=True))
    for hh, mm in ((11, 0), (16, 5)):
        monkeypatch.setattr(ledger, "_now",
                            lambda hh=hh, mm=mm: _dt.combine(D, time(hh, mm), tzinfo=ET))
        assert await recovery.run_recovery() is True, (hh, mm)
    from app.engine.safe_mode import is_safe_mode
    assert not await is_safe_mode()
