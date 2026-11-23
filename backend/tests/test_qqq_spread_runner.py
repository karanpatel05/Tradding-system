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
        return max(0, -self.net.get(s, 0)), max(0, self.net.get(l, 0))

    async def working_orders(self, symbols):
        return [dict(o) for o in self.orders.values()
                if o["status"] == "new" and {o["s"], o["l"]} & symbols]


class FakePipeline:
    def __init__(self, p):
        self.p = p

    def predict_proba(self, x):
        import numpy as np
        return np.array([[1 - self.p, self.p]])


def make(clock, feed, broker, p=0.2, capital=1500.0, model_dir=None, tmp_path=None):
    params = QqqSpreadParams(model_dir=str(model_dir))
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
                        model_loader=lambda: (FakePipeline(p), {"threshold": 0.9}),
                        safe_mode_fn=safe, alert_fn=alert_fn, log_fn=log_fn)
    r.journal, r.alerts = journal, alerts
    return r


@pytest.fixture
def model_dir(tmp_path):
    import json
    d = tmp_path / "QQQ"
    d.mkdir()
    (d / "model.joblib").write_bytes(b"x")
    (d / "model_metadata.json").write_text(json.dumps({
        "ticker": "QQQ", "features": L.FEATURES, "threshold": 0.9,
        "trained_at_utc": datetime.now().astimezone().isoformat()}))
    return tmp_path


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
async def test_full_day_hold_then_final_close(model_dir):
    clock = FakeClock(PLAN.open + timedelta(minutes=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker, p=0.2, model_dir=model_dir)

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

    # afternoon: spread decays; the model says hold at 15:30
    feed.quotes = {SHORT: {"bid": .05, "ask": .07, "mid": .06},
                   LONG: {"bid": .01, "ask": .03, "mid": .02}}
    await run_until(r, clock, PLAN.final_close - timedelta(seconds=1))
    sess = await store.get_session(KEY, DAY)
    assert sess["data"]["checkpoint"]["status"] == "hold"
    assert sess["status"] == "open"

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
async def test_checkpoint_closes_when_model_says_so(model_dir):
    clock = FakeClock(PLAN.entry - timedelta(seconds=30))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker, p=0.95, model_dir=model_dir)
    await run_until(r, clock, PLAN.checkpoint + timedelta(minutes=4))
    sess = await store.get_session(KEY, DAY)
    assert sess["data"]["checkpoint"]["status"] == "close"
    assert sess["data"]["checkpoint"]["p"] == 0.95
    assert sess["status"] == "closed"
    assert sess["data"]["closes"][0]["stage"] == "checkpoint"


@pytest.mark.asyncio
async def test_late_start_records_missed_and_sends_nothing(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(minutes=15))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker, model_dir=model_dir)
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "missed" and broker.submits == []


@pytest.mark.asyncio
async def test_routing_off_skips_without_orders(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, routing="order routing is off (ENABLE_LIVE_ORDERS)")
    r = make(clock, feed, broker, model_dir=model_dir)
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "order routing is off" in sess["reason"]
    assert broker.submits == []


@pytest.mark.asyncio
async def test_no_allocation_skips(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed)
    r = make(clock, feed, broker, capital=0.0, model_dir=model_dir)
    await r.startup()
    await r.step()
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "no capital" in sess["reason"]
    assert broker.submits == []


@pytest.mark.asyncio
async def test_unfilled_entry_is_cancelled_at_the_deadline(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    r = make(clock, feed, broker, model_dir=model_dir)
    await run_until(r, clock, PLAN.entry + timedelta(minutes=11))
    sess = await store.get_session(KEY, DAY)
    assert sess["status"] == "skipped" and "entry_unfilled" in sess["reason"]
    assert all(o["status"] == "canceled" for o in broker.orders.values())
    assert await trades() == []


@pytest.mark.asyncio
async def test_reprice_is_cancel_then_resubmit(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    r = make(clock, feed, broker, model_dir=model_dir)

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
async def test_restart_mid_entry_cancels_and_trusts_the_broker(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(minutes=2))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    sess = await store.create_session(KEY, DAY, status="entering", data={
        "signal": {"short_symbol": SHORT, "long_symbol": LONG}, "limit_credit": 0.25})
    await broker.submit_spread(SHORT, LONG, 1, 0.25, True)          # resting at Alpaca
    r = make(clock, feed, broker, model_dir=model_dir)
    await r.startup()
    sess = await store.get_session(KEY, DAY)
    assert broker.cancels == ["o1"]
    assert sess["status"] == "skipped" and "entry_unfilled" in sess["reason"]


@pytest.mark.asyncio
async def test_stopping_the_worker_cancels_its_working_order(model_dir):
    clock = FakeClock(PLAN.entry + timedelta(seconds=5))
    feed = FakeFeed(clock, day_bars())
    broker = FakeBroker(feed, fill_open=False)
    r = make(clock, feed, broker, model_dir=model_dir)
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
