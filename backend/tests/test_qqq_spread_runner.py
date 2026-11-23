"""The in-app QQQ spread runner over a whole simulated day: fake clock, fake Tastytrade
feed, fake Alpaca paper broker (fills when the limit is marketable), real session store.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_qqq_run.db python -m pytest tests/test_qqq_spread_runner.py
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.base import init_db, session_scope
from app.db.models import QqqSpreadSession, TradeRecord
from app.strategies.qqq_credit_spread import logic as L
from app.strategies.qqq_credit_spread import store
from app.strategies.qqq_credit_spread.params import QqqSpreadParams
from app.strategies.qqq_credit_spread.runner import QqqSpreadRunner

DAY = date(2026, 11, 23)          # a Monday, regular hours
PLAN = L.session_plan(DAY)
KEY = "qqq_test"
SHORT, LONG = L.occ_symbol(DAY, "P", 650.0), L.occ_symbol(DAY, "P", 649.0)


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


def day_bars(up=True, afternoon_px=None):
    """1m bars: a first hour from 648.40 to 650.60 (green -> put spread 650/649)."""
    bars, px = [], 648.40
    t = PLAN.open
    while t < PLAN.close:
        if t < PLAN.entry:
            nxt = px + (2.2 / 59 if up else -2.2 / 59)
        else:
            nxt = afternoon_px if afternoon_px is not None else px + 0.003
        bars.append({"time": int(t.timestamp() * 1000), "open": px, "high": max(px, nxt) + .05,
                     "low": min(px, nxt) - .05, "close": nxt, "volume": 50_000.0})
        px, t = nxt, t + timedelta(minutes=1)
    return bars


class FakeFeed:
    def __init__(self, clock, bars):
        self.clock, self.bars = clock, bars
        self.quotes = {SHORT: {"bid": .44, "ask": .48, "mid": .46},
                       LONG: {"bid": .20, "ask": .22, "mid": .21}}

    async def option_quotes(self, occs, expiry, max_age=30):
        return {o: dict(self.quotes[o]) for o in occs if o in self.quotes}

    async def minute_bars(self, start, end):
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        now = int(self.clock.now().timestamp() * 1000)
        return [b for b in self.bars if lo <= b["time"] < hi and b["time"] + 60_000 <= now]


class FakeBroker:
    name = "fake_alpaca"

    def __init__(self, feed, *, routing=None, fill_open=True):
        self.feed, self.routing, self.fill_open = feed, routing, fill_open
        self.orders: dict[str, dict] = {}
        self.net = {SHORT: 0, LONG: 0}
        self.ids = itertools.count(1)
        self.submits: list[dict] = []
        self.cancels: list[str] = []
        self.on_submit = None          # test hook: called after each submit
        self.fail_leg_q = 0            # make the next N ledger reads raise

    def routing_block(self):
        return self.routing

    async def submit_spread(self, s, l, qty, price, opening):
        if self.routing:
            return {"transmitted": False, "detail": self.routing}
        oid = f"o{next(self.ids)}"
        self.orders[oid] = {"id": oid, "status": "new", "qty": qty, "price": round(price, 2),
                            "opening": opening, "s": s, "l": l, "filled_qty": "0", "legs": []}
        self.submits.append(dict(self.orders[oid]))
        if self.on_submit:
            self.on_submit(len(self.submits))
        return {"transmitted": True, "order_id": oid}

    def _maybe_fill(self, o):
        if o["status"] != "new":
            return
        q = self.feed.quotes
        mid = q[o["s"]]["mid"] - q[o["l"]]["mid"]
        if o["opening"]:      # a credit limit fills at or below the mid credit
            ok = self.fill_open and o["price"] <= round(mid, 2) + 1e-9
        else:                 # a debit limit fills at or above the mid debit
            ok = o["price"] >= round(mid, 2) - 1e-9
        sp, lp = q[o["s"]]["mid"], round(q[o["s"]]["mid"] - o["price"], 4)
        if not ok:
            return
        o.update(status="filled", filled_qty=str(o["qty"]),
                 filled_avg_price=str(-o["price"] if o["opening"] else o["price"]),
                 legs=[{"symbol": o["s"], "filled_avg_price": str(sp)},
                       {"symbol": o["l"], "filled_avg_price": str(lp)}])
        sign = 1 if o["opening"] else -1
        self.net[o["s"]] -= sign * o["qty"]
        self.net[o["l"]] += sign * o["qty"]

    async def get_order(self, oid):
        o = self.orders[oid]
        self._maybe_fill(o)
        return dict(o)

    async def cancel_confirmed(self, oid, wait=6.0, step=0.25):
        o = self.orders[oid]
        self.cancels.append(oid)
        if o["status"] == "new":
            o["status"] = "canceled"
        return dict(o)

    async def leg_quantities(self, s, l):
        if self.fail_leg_q:
            self.fail_leg_q -= 1
            raise RuntimeError("Alpaca ledger read timed out")
        return max(0, -self.net.get(s, 0)), max(0, self.net.get(l, 0))

    async def working_orders(self, symbols):
        return [dict(o) for o in self.orders.values()
                if o["status"] == "new" and {o["s"], o["l"]} & symbols]


def make(clock, feed, broker, capital=1500.0):
    params = QqqSpreadParams()
    journal: list[str] = []
    alerts: list[str] = []

    async def log_fn(msg, **kw):
        journal.append(msg)

    async def alert_fn(kind, msg, data):
        alerts.append(msg)

    async def capital_fn():
        return capital

    async def safe():
        return False

    r = QqqSpreadRunner(KEY, params, feed, broker, clock, store=store, capital_fn=capital_fn,
                        safe_mode_fn=safe, alert_fn=alert_fn, log_fn=log_fn)
    r.journal, r.alerts = journal, alerts
    return r


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    await init_db()
    async with session_scope() as s:
        for row in (await s.execute(select(QqqSpreadSession).where(
                QqqSpreadSession.strategy_key == KEY))).scalars():
            await s.delete(row)
        for row in (await s.execute(select(TradeRecord).where(
                TradeRecord.strategy_key == KEY))).scalars():
            await s.delete(row)
    yield


async def run_until(r, clock, until: datetime, max_steps=400):
    await r.startup()
    for _ in range(max_steps):
        if clock.now() >= until:
            return
        await r.step()
    raise AssertionError("runner did not reach the target time")


async def trades():
    async with session_scope() as s:
        return (await s.execute(select(TradeRecord).where(
            TradeRecord.strategy_key == KEY).order_by(TradeRecord.id))).scalars().all()


@pytest.mark.asyncio
async def test_full_day_hold_then_final_close():
    clock = FakeClock(PLAN.open + timedelta(minutes=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker)

    await run_until(r, clock, PLAN.entry + timedelta(minutes=11))
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "open", sess
    assert broker.submits[0]["opening"] and broker.submits[0]["price"] == 0.25   # mid credit
    assert sess["data"]["signal"]["right"] == "P"
    assert (sess["data"]["signal"]["short_symbol"], sess["data"]["signal"]["long_symbol"]) \
        == (SHORT, LONG)
    tr = await trades()
    assert [(t.symbol, t.side, t.status, t.transmitted) for t in tr] == [
        (SHORT, "sell", "open", True), (LONG, "buy", "open", True)]

    # afternoon: the short strike comes under pressure. With no model exit nothing
    # closes before 15:50, however the spread trades.
    feed.quotes = {SHORT: {"bid": .85, "ask": .90, "mid": .875},
                   LONG: {"bid": .30, "ask": .34, "mid": .32}}
    await run_until(r, clock, PLAN.final_close - timedelta(seconds=1))
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "open" and "checkpoint" not in sess["data"]
    assert [o for o in broker.submits if not o["opening"]] == []
    feed.quotes = {SHORT: {"bid": .05, "ask": .07, "mid": .06},
                   LONG: {"bid": .01, "ask": .03, "mid": .02}}

    await run_until(r, clock, PLAN.close)
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "closed", sess
    closing = [o for o in broker.submits if not o["opening"]]
    assert closing and closing[-1]["price"] <= 1.0
    assert broker.net == {SHORT: 0, LONG: 0}
    tr = await trades()
    assert all(t.status == "closed" for t in tr)
    # sold for 0.25, bought back for the filled debit: P&L = (0.25 - debit) * 100
    debit = sess["data"]["exit_debit"]
    assert sess["realized_pnl"] == pytest.approx((0.25 - debit) * 100, abs=0.01)
    assert not r.alerts


@pytest.mark.asyncio
async def test_late_start_records_missed_and_sends_nothing():
    clock = FakeClock(PLAN.entry + timedelta(minutes=15))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker)
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "missed" and broker.submits == []


@pytest.mark.asyncio
async def test_routing_off_skips_without_orders():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, routing="order routing is off (ENABLE_LIVE_ORDERS)")
    r = make(clock, feed, broker)
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "order routing is off" in sess["reason"]
    assert broker.submits == []


@pytest.mark.asyncio
async def test_no_allocation_skips():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker, capital=0.0)
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "no capital" in sess["reason"]
    assert broker.submits == []


@pytest.mark.asyncio
async def test_unfilled_entry_is_cancelled_at_the_deadline():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    r = make(clock, feed, broker)
    await run_until(r, clock, PLAN.entry + timedelta(minutes=11))
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "entry_unfilled" in sess["reason"]
    assert all(o["status"] == "canceled" for o in broker.orders.values())
    assert await trades() == []


@pytest.mark.asyncio
async def test_reprice_is_cancel_then_resubmit():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    r = make(clock, feed, broker)

    def market_moves(n):
        if n == 1:          # right after the first order: mid credit drops to 0.21
            feed.quotes[SHORT] = {"bid": .40, "ask": .44, "mid": .42}
            broker.fill_open = True
    broker.on_submit = market_moves
    await r.startup()
    await r.step()
    assert [o["price"] for o in broker.submits] == [0.25, 0.21]
    assert broker.cancels[0] == "o1"
    assert (await store.get_session(KEY, DAY))["status"] == "open"


@pytest.mark.asyncio
async def test_restart_mid_entry_cancels_and_trusts_the_broker():
    clock = FakeClock(PLAN.entry + timedelta(minutes=2))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    sess = await store.create_session(KEY, DAY, status="entering", data={
        "signal": {"short_symbol": SHORT, "long_symbol": LONG}, "limit_credit": 0.25})
    await broker.submit_spread(SHORT, LONG, 1, 0.25, True)          # resting at Alpaca
    r = make(clock, feed, broker)
    await r.startup()
    sess = await store.get_session(KEY, DAY)
    assert broker.cancels == ["o1"]
    assert sess["status"] == "skipped" and "entry_unfilled" in sess["reason"]


@pytest.mark.asyncio
async def test_stopping_the_worker_cancels_its_working_order():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    r = make(clock, feed, broker)
    submitted = asyncio.Event()
    broker.on_submit = lambda n: submitted.set()
    real_sleep = clock.sleep

    async def slow_sleep(seconds):          # let the test cancel mid-chase
        await real_sleep(seconds)
        await asyncio.sleep(0.01)
    clock.sleep = slow_sleep
    await r.startup()
    task = asyncio.create_task(r.step())
    await asyncio.wait_for(submitted.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.orders["o1"]["status"] == "canceled"


async def run_like_forever(r, clock, until, max_steps=600):
    """run_forever's error handling (log, back off, retry) without the endless loop."""
    from app.strategies.qqq_credit_spread.runner import ERROR_BACKOFF
    await r.startup()
    for _ in range(max_steps):
        if clock.now() >= until:
            return
        try:
            await r.step()
        except Exception:  # noqa: BLE001
            await clock.sleep(ERROR_BACKOFF)
    raise AssertionError("runner did not reach the target time")


@pytest.mark.asyncio
async def test_ledger_errors_at_entry_settle_and_final_close_still_end_flat():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker)
    broker.fail_leg_q = 1                     # the read right after the entry fill fails
    await run_like_forever(r, clock, PLAN.entry + timedelta(minutes=12))
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "open", sess     # resolved on the next step, not re-entered
    assert len([o for o in broker.submits if o["opening"]]) == 1
    assert [t.status for t in await trades()] == ["open", "open"]

    await run_like_forever(r, clock, PLAN.final_close - timedelta(seconds=1))
    broker.fail_leg_q = 1                     # the final close's first ledger read fails
    await run_like_forever(r, clock, PLAN.close)
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "closed", sess
    assert broker.net == {SHORT: 0, LONG: 0}
    assert all(t.status == "closed" for t in await trades())


@pytest.mark.asyncio
async def test_entry_guard_blocks_new_entries_only():
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker)
    r.entry_guard_fn = lambda: "com.karan.spreadlive still runs with --alpaca-paper"
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "--alpaca-paper" in sess["reason"]
    assert broker.submits == []
