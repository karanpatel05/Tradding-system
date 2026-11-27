"""The ETF fly must close through confirmed orders, including on a shortened session."""
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.base import init_db, session_scope
from app.db.models import SystemFlag, TradeRecord, XspFlySession
from app.strategies.xsp_iron_fly import calendar, store
from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaFlyExecutor, option_tick
from app.strategies.xsp_iron_fly.feed import Contract, Quote, Spot
from app.strategies.xsp_iron_fly.params import IronFlyParams
from app.strategies.xsp_iron_fly.runner import IronFlyRunner
from tests.test_xsp_fly_sim import FakeFeed, SimClock, at, flat
from tests.xsp_fakes import FakeAlpaca, TestExec


KEY = "spy_iron_fly"
MONDAY = date(2026, 11, 23)
EARLY_CLOSE = date(2026, 11, 27)


class EtfFeed:
    def __init__(self, clock):
        self.clock = clock
        self.no_bid = None

    def spot(self, _):
        return Spot(600.1, self.clock.now())

    def data_ready(self):
        return True

    async def subscribe(self, _):
        pass

    async def contracts(self, ticker, d):
        chain = []
        for right, strike in (("P", 590), ("P", 600), ("C", 600), ("C", 610)):
            sym = f"{ticker}{d:%y%m%d}{right}{strike * 1000:08d}"
            chain.append(Contract(right, strike, sym, sym, calendar.close_time(d)))
        return chain

    def quote(self, symbol):
        if self.no_bid == symbol and self.clock.now().hour >= 15:
            return Quote(0, 0.05, self.clock.now())
        bid = 3.05 if symbol.endswith("00600000") else 0.45
        return Quote(bid, bid + 0.04, self.clock.now())

    async def settlement_price(self, *args):
        raise AssertionError("SPY must never request an index cash settlement")


class EtfBroker(FakeAlpaca):
    blocked_short = None

    def _touch(self, sym):
        q = self.feed.quote(sym)
        return q.bid, q.ask

    async def submit(self, body, intent, qty):
        if body.get("symbol") == self.blocked_short and body.get("position_intent") == "buy_to_close":
            self.bodies.append(body)
            return {"transmitted": False, "detail": "close rejected by test broker"}
        return await super().submit(body, intent, qty)


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    await init_db()
    async with session_scope() as db:
        await db.execute(delete(XspFlySession))
        await db.execute(delete(TradeRecord).where(TradeRecord.strategy_key.in_((KEY, "xsp_iron_fly"))))
        await db.execute(delete(SystemFlag).where(SystemFlag.key.in_(
            ("spy_fly_halt", store.HALT_FLAG, "safe_mode"))))


def setup_day(d=MONDAY, **changes):
    clock = SimClock(at(d, 9, 45))
    feed = EtfFeed(clock)
    broker = EtfBroker(feed)
    ex = AlpacaFlyExecutor(broker, feed, clock, window=2, step=0.5)
    params = replace(IronFlyParams(), ticker="SPY", close_before_settlement=True,
                     flatten_minutes_before_close=30, monitor_interval=60, close_retry_seconds=60, **changes)

    async def equity():
        return 5000

    async def safe():
        return False

    runner = IronFlyRunner(KEY, params, feed, ex, clock, equity_fn=equity, safe_mode_fn=safe)
    return runner, broker, feed, clock


@pytest.mark.asyncio
@pytest.mark.parametrize("d", [MONDAY, EARLY_CLOSE])
async def test_close_uses_broker_fills_before_regular_or_early_close(d):
    # Override the half-day entry guard to exercise close scheduling for a held fly.
    runner, broker, _, _ = setup_day(d, min_minutes_to_close=120)
    s = await runner.run_session(d)
    assert s["status"] == "closed" and s["settlement_price"] is None
    exits = [f for f in s["fills"] if f["kind"] == "pre_settlement"]
    assert len(exits) == 4 and all(f["order_id"] for f in exits)
    close_time = calendar.close_time(d)
    assert close_time - timedelta(minutes=30) <= datetime.fromisoformat(exits[0]["ts"]) < close_time
    assert [f["side"] for f in exits] == ["BUY", "BUY", "SELL", "SELL"]
    assert s["fees"] == 8 and s["reconciliation"]["ok"]
    assert s["realized_pnl"] == pytest.approx(-24.0)
    assert await broker.positions() == {}
    assert option_tick(3.07, "SPY") == 0.01
    first_close = next(b for b in broker.bodies if b.get("position_intent") == "buy_to_close")
    assert first_close["limit_price"] == "3.14"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["short_rejected", "wing_no_bid", "close_unknown", "broker_mismatch"])
async def test_failed_close_keeps_record_open_for_assignment_review(failure):
    runner, broker, feed, clock = setup_day()
    short = "SPY261123C00600000"
    wing = "SPY261123C00610000"
    if failure == "short_rejected":
        broker.blocked_short = short
    elif failure == "wing_no_bid":
        feed.no_bid = wing
    elif failure == "close_unknown":
        broker.unreadable_intents.add("buy_to_close")
    else:
        broker.extra[wing] = 1
    s = await runner.run_session(MONDAY)
    assert s["status"] == "error" and s["realized_pnl"] is None and s["settlement_price"] is None
    assert await store.halt_reason(key=KEY) and await store.halt_reason() is None
    assert all(f["kind"] not in ("abandon", "settle") for f in s["fills"])
    if failure in ("short_rejected", "close_unknown"):
        assert not any(b.get("symbol") == wing and b.get("side") == "sell" for b in broker.bodies)
    day2 = MONDAY + timedelta(days=1)
    clock.t = at(day2, 9, 45)
    orders_before = len(broker.bodies)
    await runner.recover_stale(day2)
    await store.clear_halt(key=KEY)
    skipped = await runner.run_session(day2)
    assert skipped["status"] == "skipped" and skipped["ticker"] == "SPY"
    assert "reconciliation" in skipped["reason"] and len(broker.bodies) == orders_before


@pytest.mark.asyncio
async def test_legacy_exit_only_worker_never_opens_a_new_session():
    runner, _, _, _ = setup_day()
    legacy = IronFlyRunner("xsp_iron_fly", IronFlyParams(), runner.feed, runner.ex, runner.clock,
                           equity_fn=runner.equity_fn, safe_mode_fn=runner.safe_mode_fn)
    await legacy.run_forever(manage_only=True)
    assert await store.get_session("xsp_iron_fly", MONDAY) is None


@pytest.mark.asyncio
async def test_legacy_exit_only_worker_finishes_existing_fly_and_returns():
    runner, _, _, clock = setup_day()
    feed = FakeFeed(clock, flat())
    executor = TestExec(feed, clock, port=FakeAlpaca(feed))
    legacy = IronFlyRunner("xsp_iron_fly", replace(IronFlyParams(), monitor_interval=60), feed,
                           executor, clock, equity_fn=runner.equity_fn, safe_mode_fn=runner.safe_mode_fn)
    assert (await legacy._enter(MONDAY, calendar.close_time(MONDAY)))["status"] == "open"
    clock.t = at(MONDAY, 15, 58)
    await legacy.run_forever(manage_only=True)
    assert not await store.has_unfinished("xsp_iron_fly")
    assert (await store.get_session("xsp_iron_fly", MONDAY))["status"] == "settled"
    assert await store.get_session("xsp_iron_fly", MONDAY + timedelta(days=1)) is None


@pytest.mark.asyncio
async def test_restart_after_etf_expiry_preserves_assignable_exposure():
    runner, broker, _, clock = setup_day()
    assert (await runner._enter(MONDAY, calendar.close_time(MONDAY)))["status"] == "open"
    before = len(broker.bodies)
    clock.t = at(MONDAY, 16, 5)
    session = await runner.run_session(MONDAY)
    assert session["status"] == "error" and session["realized_pnl"] is None
    assert len(broker.bodies) == before and len(await broker.positions()) == 4


@pytest.mark.asyncio
async def test_pause_during_monitoring_finishes_closes_and_prevents_next_entry():
    runner, broker, _, clock = setup_day()
    checks = []

    async def paused():
        checks.append(clock.now())
        return clock.now() >= at(MONDAY, 10)

    runner.entries_paused_fn = paused
    await runner.run_forever()
    assert (await store.get_session(KEY, MONDAY))["status"] == "closed"
    assert await broker.positions() == {} and clock.now().date() == MONDAY
    assert len(checks) == 3  # before planning, after claiming entry, after managing


@pytest.mark.asyncio
async def test_pause_racing_an_entry_plan_prevents_first_order():
    runner, broker, _, _ = setup_day()

    async def paused():
        return await store.get_session(KEY, MONDAY) is not None

    runner.entries_paused_fn = paused
    await runner.run_forever()
    assert (await store.get_session(KEY, MONDAY))["status"] == "skipped"
    assert not broker.bodies
