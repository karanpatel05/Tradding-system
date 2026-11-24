"""XSP iron fly on Alpaca paper (fake Alpaca): real-order executor semantics, the runner
end to end in alpaca_paper mode, restart recovery, EOD reconciliation, the expiry-aware
ledger, and activation gating. No network.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_xspa.db ../.venv/bin/python -m pytest tests/test_xsp_fly_alpaca.py
"""
from __future__ import annotations

import asyncio
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
from tests import xsp_fakes
from tests.xsp_fakes import FakeAlpaca

ET = ZoneInfo("America/New_York")
KEY = "xsp_iron_fly"
PA = replace(P, mode="alpaca_paper")


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    xsp_fakes.reset()
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
async def test_stop_close_is_single_leg_buy_to_close_on_the_tick():
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
    lim = float(b["limit_price"])
    assert lim >= q.ask + 0.03 - 1e-9                                 # rounded up
    if lim >= 3.0:
        assert round(lim * 100) % 5 == 0                              # nickel from $3
    assert res.status == "filled" and res.prices["short_call"] == pytest.approx(q.ask)


def test_option_tick_penny_under_3_nickel_above():
    from app.strategies.xsp_iron_fly.alpaca_executor import option_tick
    assert option_tick(0.03) == 0.01 and option_tick(2.99) == 0.01
    assert option_tick(3.00) == 0.05 and option_tick(7.2) == 0.05


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
    alp.unreadable_intents = {"buy_to_close"}   # the entry reads fine; the stop does not
    s = await _runner(clock, feed, alp).run_session(D)
    assert s["status"] == "error" and "stop close for short_call state unknown" in s["reason"]
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
async def test_legacy_sim_session_is_never_managed_by_the_alpaca_executor():
    """An open fly recorded by the old simulator (mode "sim") must not get orders sent
    to Alpaca: it is left untouched with an alert."""
    clock = SimClock(at(D, 11, 0))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    for l in legs:
        l.update(qty=1, status="open", entry_price=1.0)
    await store.create_session(KEY, D, status="open", mode="sim", legs=legs,
                               close_time=at(D, 16, 0), contracts=1)
    alp = FakeAlpaca(feed)
    alerts = []
    s = await _runner(clock, feed, alp, alerts=alerts).run_session(D)
    assert s["status"] == "mode_mismatch" and alp.bodies == []
    assert any(k == "xsp_fly_mode_mismatch" for k, _ in alerts)
    assert (await store.get_session(KEY, D))["status"] == "open"           # untouched


# --- ledger expiry --------------------------------------------------------------
def test_option_expired_rule_cash_settled_roots_only():
    from datetime import date as _d
    t = lambda d, hh, mm, ss=0: datetime.combine(d, time(hh, mm, ss), tzinfo=ET)  # noqa: E731
    sym = "XSP261123C00600000"
    assert not ledger.option_expired(sym, t(D, 15, 59, 59))
    assert ledger.option_expired(sym, t(D, 16, 0, 0))
    assert ledger.option_expired(sym, t(D + timedelta(days=1), 9, 0))
    assert ledger.option_expired("SPXW261123P05000000", t(D, 16, 1))
    # 23:30 ET is 04:30 UTC the next day: the ET date still decides.
    from datetime import timezone as _tz
    late = t(D - timedelta(days=1), 23, 30).astimezone(_tz.utc)
    assert not ledger.option_expired(sym, late)
    # Physically settled equity/ETF options are never dropped (assignment into shares).
    assert not ledger.option_expired("QQQ261123P00590000", t(D + timedelta(days=3), 9, 0))
    assert not ledger.option_expired("SPY", t(D + timedelta(days=9), 9, 0))
    # Half day (2026-11-27 closes 13:00 ET).
    hd = _d(2026, 11, 27)
    assert not ledger.option_expired("XSP261127C00600000", t(hd, 12, 59))
    assert ledger.option_expired("XSP261127C00600000", t(hd, 13, 0))


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
    # XSP's local side comes from its session's own order fills.
    legs = [{"role": f"r{i}", "symbol": k, "occ": k, "action": side.upper(), "qty": 1}
            for i, (k, (side, _)) in enumerate(syms.items())]
    fills = [{"symbol": k, "side": side.upper(), "qty": 1, "kind": "entry"}
             for k, (side, _) in syms.items()]
    sess = await store.create_session(KEY, D, status="open", mode="alpaca_paper",
                                      legs=legs, fills=fills)
    for hh, mm in ((11, 0), (16, 5)):
        monkeypatch.setattr(ledger, "_now",
                            lambda hh=hh, mm=mm: _dt.combine(D, time(hh, mm), tzinfo=ET))
        assert await recovery.run_recovery() is True, (hh, mm)
    # After the strategy books the settlement (session settled, leg TradeRecords
    # closed): still clean, the same evening and the next morning.
    sess["status"] = "settled"
    await store.save(sess)
    async with session_scope() as db:
        for tr in (await db.execute(select(TradeRecord).where(
                TradeRecord.strategy_key == KEY))).scalars().all():
            tr.status = "closed"
    for day, hh in ((D, 16), (D + timedelta(days=1), 9)):
        monkeypatch.setattr(ledger, "_now",
                            lambda day=day, hh=hh: _dt.combine(day, time(hh, 30), tzinfo=ET))
        assert await recovery.run_recovery() is True, (day, hh)
    from app.engine.safe_mode import is_safe_mode
    assert not await is_safe_mode()



# --- shared order path ------------------------------------------------------------
@pytest.fixture
def routing_on(monkeypatch):
    from app.broker import orders
    from app.config import get_settings
    st = get_settings()
    monkeypatch.setattr(st, "apca_api_key_id", "k")
    monkeypatch.setattr(st, "apca_api_secret_key", "x")
    monkeypatch.setattr(st, "apca_api_base_url", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(st, "enable_live_orders", True)
    sent = []

    async def fake_transmit(body, ref, what):
        sent.append(body)
        return {"transmitted": True, "order_id": "id1", "ref": ref}

    async def not_safe():
        return False

    async def flat_ledger(**kw):
        return {}
    monkeypatch.setattr(orders, "_transmit", fake_transmit)
    monkeypatch.setattr(orders, "is_safe_mode", not_safe)
    monkeypatch.setattr(ledger, "app_net_positions", flat_ledger)
    return sent


@pytest.mark.asyncio
async def test_port_sends_the_fly_through_submit_combo_order_with_a_negative_credit(routing_on):
    """Pins the credit sign confirmed on this paper account by the 2026-11-24 test order
    (ef205d21...): Alpaca stored limit_price "-9.5" for a SELL fly and it rested
    unfilled, i.e. a NEGATIVE limit is the least credit accepted."""
    from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaPort
    body = {"order_class": "mleg", "qty": "1", "type": "limit", "time_in_force": "day",
            "limit_price": "-9.50",
            "legs": [{"symbol": "XSP261124C00715000", "side": "sell"},
                     {"symbol": "XSP261124P00715000", "side": "sell"},
                     {"symbol": "XSP261124C00725000", "side": "buy"},
                     {"symbol": "XSP261124P00705000", "side": "buy"}]}
    out = await AlpacaPort(KEY).submit(body, "fly_open", 1)
    assert out["transmitted"] and out["order_id"] == "id1"
    (sent,) = routing_on
    assert sent["order_class"] == "mleg" and sent["limit_price"] == "-9.5"
    assert [(x["symbol"], x["side"], x["position_intent"]) for x in sent["legs"]] == [
        ("XSP261124C00715000", "sell", "sell_to_open"),
        ("XSP261124P00715000", "sell", "sell_to_open"),
        ("XSP261124C00725000", "buy", "buy_to_open"),
        ("XSP261124P00705000", "buy", "buy_to_open")]


@pytest.mark.asyncio
async def test_single_leg_helper_is_a_tagged_limit_and_closes_in_safe_mode(routing_on,
                                                                          monkeypatch):
    from app.broker import orders
    out = await orders.submit_option_order("XSP261124C00715000", "BUY", 1, limit_price=3.456,
                                           position_intent="buy_to_close",
                                           strategy_key=KEY, reason="stop")
    assert out["transmitted"]
    b = routing_on[-1]
    assert (b["symbol"], b["side"], b["type"], b["limit_price"], b["time_in_force"],
            b["position_intent"]) == ("XSP261124C00715000", "buy", "limit", "3.46", "day",
                                      "buy_to_close")

    async def safe():
        return True

    async def held(**kw):
        return {"XSP261124C00715000": -1}
    monkeypatch.setattr(orders, "is_safe_mode", safe)
    monkeypatch.setattr(ledger, "app_net_positions", held)
    assert (await orders.submit_option_order(
        "XSP261124C00715000", "BUY", 1, limit_price=3.5,
        position_intent="buy_to_close"))["transmitted"]                 # a close passes
    blocked = await orders.submit_option_order("XSP261124C00715000", "SELL", 1,
                                               limit_price=3.5,
                                               position_intent="sell_to_open")
    assert not blocked["transmitted"] and blocked.get("safe_mode")      # an open doesn't
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    off = await orders.submit_option_order("XSP261124C00715000", "BUY", 1, limit_price=3.5,
                                           position_intent="buy_to_close")
    assert not off["transmitted"] and "ENABLE_LIVE_ORDERS" in off["detail"]


# --- BACKEND review round 2 -------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["missing", "error"])
async def test_unresolved_post_state_is_unknown_never_rejected(lookup):
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.unknown, alp.lookup = True, lookup
    ex = ex_for(clock, feed, alp)
    res = await ex.combo(legs, 1, 0.5, max_age=5)
    assert res.status == "unknown" and "state unknown" in res.detail
    res = await ex.single(legs[0], "BUY", 1, 5.0, max_age=30)
    assert res.status == "unknown"


@pytest.mark.asyncio
async def test_transmitted_without_an_order_id_is_unknown():
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.no_id = True
    res = await ex_for(clock, feed, alp).combo(legs, 1, 0.5, max_age=5)
    assert res.status == "unknown" and "without an order id" in res.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stopped", "suspended", "pending_cancel", ""])
async def test_non_terminal_or_unexpected_status_is_never_read_as_unfilled(status):
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    alp = FakeAlpaca(feed)
    alp.hold_status = status
    res = await ex_for(clock, feed, alp).combo(legs, 1, 0.5, max_age=5)
    assert res.status == "unknown"


@pytest.mark.asyncio
async def test_unknown_entry_trips_safe_mode_and_opens_nothing():
    from app.engine.safe_mode import is_safe_mode
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    alp.unknown, alp.lookup = True, "error"
    s = await _runner(clock, feed, alp).run_session(D)
    assert s["status"] == "error" and "state unknown" in s["reason"]
    assert await is_safe_mode() and len(alp.bodies) == 1     # never resent


@pytest.mark.asyncio
async def test_partial_single_leg_stop_closes_over_ticks():
    from app.strategies.xsp_iron_fly.params import IronFlyParams  # noqa: F401
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    feed = FakeFeed(clock, path)
    alp = await _bound_fake(clock, feed)
    params = replace(PA, max_daily_loss_per_contract=2500)
    runner = _runner(clock, feed, alp, params, equity=10_000)
    real_submit = alp.submit

    async def one_per_stop(body, intent, qty):           # each stop order fills 1 only
        if body.get("position_intent") == "buy_to_close":
            alp.fill_cap, alp.fill_once = 1, True
        else:
            alp.fill_cap, alp.fill_once = None, False
        return await real_submit(body, intent, qty)
    alp.submit = one_per_stop
    s = await runner.run_session(D)
    sc = next(l for l in s["legs"] if l["role"] == "short_call")
    assert s["contracts"] == 2 and sc["status"] == "stopped" and sc["exit_qty"] == 2
    stops = [f for f in s["fills"] if f["kind"] == "stop" and f["role"] == "short_call"]
    assert len(stops) == 2 and all(f["qty"] == 1 and f["order_id"] for f in stops)
    assert s["reconciliation"]["ok"], s["reconciliation"]


@pytest.mark.asyncio
async def test_restart_fails_closed_when_working_orders_cannot_be_listed():
    from app.engine.safe_mode import is_safe_mode
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    alp = await _bound_fake(clock, feed)
    await _crash_at(_runner(clock, feed, alp).run_session(D), clock, at(D, 11, 0))
    alp.list_error = True
    feed2 = FakeFeed(clock, flat(600.0))
    await feed2.contracts("XSP", D)
    s = await _runner(clock, feed2, alp).run_session(D)
    assert s["status"] == "error" and "could not list open Alpaca orders" in s["reason"]
    assert await is_safe_mode()


@pytest.mark.asyncio
async def test_error_session_settles_next_day_once_alpaca_agrees():
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    feed = FakeFeed(clock, path)
    alp = await _bound_fake(clock, feed)
    alp.unreadable_intents = {"buy_to_close"}   # the stop's order can't be read -> error
    s = await _runner(clock, feed, alp).run_session(D)
    assert s["status"] == "error" and "stop close" in s["reason"]
    async with session_scope() as db:          # a human clears SAFE_MODE
        await db.execute(delete(SystemFlag).where(SystemFlag.key == "safe_mode"))
    (stop_oid,) = alp.unreadable_ids             # the stop did fill at Alpaca...
    alp.unreadable_ids.clear()
    alp.orders[stop_oid].update(status="filled", filled_qty="1", filled_avg_price="4.0")
    # ...but the session never recorded it: Alpaca disagrees, so it stays an error.
    nxt = D + timedelta(days=1)
    clock.t = at(nxt, 8, 0)
    feed2 = FakeFeed(clock, path)
    await feed2.contracts("XSP", D)
    await _runner(clock, feed2, alp).recover_stale(nxt)
    assert (await store.get_session(KEY, D))["status"] == "error"
    # Once Alpaca and the session agree (the stray order never filled), it settles.
    alp.orders[stop_oid]["filled_qty"] = "0"
    alp.orders[stop_oid]["status"] = "canceled"
    await _runner(clock, feed2, alp).recover_stale(nxt)
    s = await store.get_session(KEY, D)
    assert s["status"] == "settled" and "resolved after error" in s["reason"]
    async with session_scope() as db:
        trs = (await db.execute(select(TradeRecord).where(
            TradeRecord.strategy_key == KEY))).scalars().all()
    assert trs and all(t.status == "closed" for t in trs)


@pytest.mark.asyncio
async def test_engine_recovery_mid_entry_counts_session_fills_not_trade_records(monkeypatch):
    """Restart mid-entry: 4 legs filled at Alpaca, no TradeRecords yet -> clean."""
    from datetime import datetime as _dt

    from app.config import get_settings
    from app.engine import recovery
    st = get_settings()
    monkeypatch.setattr(st, "apca_api_key_id", "k")
    monkeypatch.setattr(st, "apca_api_secret_key", "x")
    monkeypatch.setattr(recovery, "_schedule_backfill", lambda: None)
    monkeypatch.setattr(ledger, "_now", lambda: _dt.combine(D, time(10, 0), tzinfo=ET))
    clock = SimClock(at(D, 9, 45))
    feed = FakeFeed(clock, flat(600.0))
    legs = await _legs(clock, feed)
    fills = [{"symbol": l["symbol"], "side": l["action"], "qty": 1, "kind": "entry"}
             for l in legs]
    await store.create_session(KEY, D, status="entering", mode="alpaca_paper",
                               legs=[dict(l, qty=1) for l in legs], fills=fills)
    held = {occ(l): (-1 if l["action"] == "SELL" else 1) for l in legs}
    order = {"order_class": "mleg", "client_order_id": "tsys-x", "status": "filled",
             "legs": [{"symbol": k, "side": "sell" if q < 0 else "buy", "filled_qty": "1"}
                      for k, q in held.items()]}

    async def app_orders(status="all"):
        return [order]

    async def account_positions():
        return {k: {"qty": q, "avg_entry_price": 1.0} for k, q in held.items()}
    monkeypatch.setattr(ledger, "app_orders", app_orders)
    monkeypatch.setattr(ledger, "account_positions", account_positions)
    async with session_scope() as db:
        await db.execute(delete(TradeRecord).where(TradeRecord.transmitted.is_(True)))
    assert await recovery.run_recovery() is True


def test_resume_ignores_routing_but_not_invalid_settings(monkeypatch):
    from app.config import get_settings
    from app.strategies.xsp_iron_fly import XspIronFlyStrategy
    from app.strategies.xsp_iron_fly.params import IronFlyParams
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    s = XspIronFlyStrategy(IronFlyParams())
    assert s.activation_block_reason() and s.resume_block_reason() is None
    assert XspIronFlyStrategy(IronFlyParams(mode="sim")).resume_block_reason()


@pytest.mark.asyncio
async def test_safe_mode_close_is_capped_at_the_apps_quantity(routing_on, monkeypatch):
    from app.broker import orders

    async def safe():
        return True

    async def held(**kw):
        return {"XSP261124C00715000": -1}
    monkeypatch.setattr(orders, "is_safe_mode", safe)
    monkeypatch.setattr(ledger, "app_net_positions", held)
    out = await orders.submit_option_order("XSP261124C00715000", "BUY", 3, limit_price=3.5,
                                           position_intent="buy_to_close")
    assert out["transmitted"] and routing_on[-1]["qty"] == "1"        # capped 3 -> 1
    none = await orders.submit_option_order("XSP261124P00715000", "BUY", 1, limit_price=3.5,
                                            position_intent="buy_to_close")
    assert not none["transmitted"] and "holds nothing" in none["detail"]


@pytest.mark.asyncio
async def test_real_port_lookup_tells_404_from_errors(monkeypatch):
    import httpx

    from app.broker import alpaca_client as ac
    from app.broker.alpaca_client import AlpacaError
    from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaPort
    port = AlpacaPort(KEY)

    async def r404(*a, **k):
        raise AlpacaError(404, "order not found")

    async def r500(*a, **k):
        raise AlpacaError(500, "boom")

    async def rnet(*a, **k):
        raise httpx.ConnectError("down")

    async def rok(*a, **k):
        return {"id": "abc"}
    for fn, want in ((r404, "missing"), (r500, "error"), (rnet, "error"), (rok, "found")):
        monkeypatch.setattr(ac, "request", fn)
        assert (await port.find_by_ref("x"))[0] == want
