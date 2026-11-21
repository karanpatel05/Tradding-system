"""XSP iron fly runner on simulated time: a fake feed (Black-Scholes mids, scripted spot
paths, per-symbol quote overrides) and the real SimExecutor, store and DB.

These exercise the full live code path (entry, per-leg stops incl. gap-through,
settlement, guards, breakers, restart/kill recovery, multi-session) without network.
They are NOT a substitute for a paper run over real sessions.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_xsp.db ../.venv/bin/python -m pytest tests/test_xsp_fly_sim.py
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.base import init_db, session_scope
from app.db.models import SystemFlag, TradeRecord, XspFlySession
from app.options.blackscholes import bs_price
from app.strategies.xsp_iron_fly import calendar as cal
from app.strategies.xsp_iron_fly import logic as L
from app.strategies.xsp_iron_fly import store
from app.strategies.xsp_iron_fly.executor import OrderResult, SimExecutor
from app.strategies.xsp_iron_fly.feed import Contract, Quote, Spot
from app.strategies.xsp_iron_fly.params import IronFlyParams
from app.strategies.xsp_iron_fly.runner import IronFlyRunner, reconcile_session

ET = ZoneInfo("America/New_York")
KEY = "xsp_iron_fly"
D = date(2026, 11, 23)                      # a Monday, regular session


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET)


class SimClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t = self.t + timedelta(seconds=max(0.0, s))
        await asyncio.sleep(0)


class FakeFeed:
    """Spot from ``path(t)``; option mids from Black-Scholes on that spot with time to
    the close; bid/ask = mid -/+ half_spread. ``overrides[(role_symbol)] = fn(t)``
    returning (bid, ask), (bid, ask, receive_ts) or None replaces a quote (gaps, wide
    spreads, frozen feeds)."""

    def __init__(self, clock, path, *, iv=0.30, half_spread=0.02, has_expiry=True,
                 spot_stale_until: datetime | None = None, settle_none=False) -> None:
        self.clock, self.path, self.iv, self.hs = clock, path, iv, half_spread
        self.has_expiry = has_expiry
        self.spot_stale_until = spot_stale_until
        self.settle_none = settle_none
        self.overrides: dict[str, object] = {}
        self.meta: dict[str, tuple[str, float, date]] = {}

    def sym(self, d: date, right: str, k: float) -> str:
        return f".XSP{d:%y%m%d}{right}{k:g}"

    async def contracts(self, ticker, expiry):
        if not self.has_expiry:
            return []
        s0 = round(self.path(at(expiry, 9, 45)))
        out = []
        for k in range(s0 - 40, s0 + 41):
            for r in ("C", "P"):
                sym = self.sym(expiry, r, k)
                self.meta[sym] = (r, float(k), expiry)
                out.append(Contract(r, float(k), sym, f"XSP {expiry:%y%m%d}{r}{k:08d}"))
        return out

    async def subscribe(self, symbols):
        return None

    def data_ready(self):
        return True

    def spot(self, ticker):
        now = self.clock.now()
        ts = now
        if self.spot_stale_until and now < self.spot_stale_until:
            ts = now - timedelta(seconds=60)
        return Spot(self.path(now), ts)

    def quote(self, symbol):
        now = self.clock.now()
        fn = self.overrides.get(symbol)
        if fn is not None:
            o = fn(now)
            if o is not None:
                return Quote(o[0], o[1], o[2] if len(o) > 2 else now)
        r, k, exp = self.meta[symbol]
        close = cal.close_time(exp)
        T = max((close - now).total_seconds(), 0) / (365 * 24 * 3600)
        m = bs_price(self.path(now), k, T, self.iv, 0.0, r)
        return Quote(max(0.0, round(m - self.hs, 2)), round(m + self.hs, 2), now)

    async def settlement_price(self, ticker, d, close_dt, now):
        if self.settle_none or now < close_dt:
            return None
        return self.path(close_dt)


P = IronFlyParams(monitor_interval=2.0, fees_per_contract=1.0)


def flat(level=600.2):
    return lambda t: level


def ramp(t0: datetime, t1: datetime, a: float, b: float):
    def f(t):
        if t <= t0:
            return a
        if t >= t1:
            return b
        return a + (b - a) * (t - t0).total_seconds() / (t1 - t0).total_seconds()
    return f


def make(clock, feed, params=P, *, capital=5000.0, executor=None, alerts=None,
         safe=False):
    async def cap():
        return capital

    async def sm():
        return safe

    async def alert(kind, msg, data=None):
        if alerts is not None:
            alerts.append((kind, msg))

    return IronFlyRunner(KEY, params, feed, executor or SimExecutor(feed, clock), clock,
                         capital_fn=cap, safe_mode_fn=sm, alert_fn=alert)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with session_scope() as s:
        await s.execute(delete(XspFlySession))
        await s.execute(delete(TradeRecord).where(TradeRecord.strategy_key == KEY))
        await s.execute(delete(SystemFlag).where(SystemFlag.key == store.HALT_FLAG))
    yield


async def trades():
    async with session_scope() as s:
        return (await s.execute(select(TradeRecord).where(
            TradeRecord.strategy_key == KEY).order_by(TradeRecord.id))).scalars().all()


def leg(s, role):
    return next(l for l in s["legs"] if l["role"] == role)


def entry_fills(s):
    return [f for f in s["fills"] if f["kind"] == "entry"]


# --- full days ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quiet_day_settles_for_most_of_the_credit():
    clock = SimClock(at(D, 9, 0))
    s = await make(clock, FakeFeed(clock, flat(600.2))).run_session(D)
    assert s["status"] == "settled", s["reason"]
    assert s["body_strike"] == 600.0
    assert [(l["role"], l["strike"]) for l in s["legs"]] == [
        ("short_call", 600.0), ("short_put", 600.0), ("long_call", 610.0), ("long_put", 590.0)]
    # Walks mid -> natural in 0.05 steps every 5 s; fills at the second concession.
    assert at(D, 9, 45) <= s["entry_time"] <= at(D, 9, 45, 35)
    assert all(l["status"] == "settled" for l in s["legs"])
    assert s["settlement_price"] == pytest.approx(600.2)
    gross = L.fly_settlement_pnl(s["legs"], 600.2, 1)
    assert s["realized_pnl"] == pytest.approx(gross - 4.0, abs=0.02)   # 4 entry fees
    assert s["realized_pnl"] > 0
    assert s["max_loss"] == pytest.approx((10 - s["net_credit"]) * 100, abs=0.01)
    assert s["reconciliation"]["ok"], s["reconciliation"]
    trs = await trades()
    assert len(trs) == 4 and all(t.status == "closed" and not t.transmitted for t in trs)
    assert sum(t.pnl for t in trs) == pytest.approx(s["realized_pnl"], abs=0.02)
    # Entry filled at the natural (pessimistic) and slippage vs mid was logged per fill.
    assert all(f["slippage_vs_mid"] == pytest.approx(0.02) for f in entry_fills(s))


@pytest.mark.asyncio
async def test_one_short_stops_other_runs_wings_held():
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    s = await make(clock, FakeFeed(clock, path)).run_session(D)
    sc, sp, lc, lp = (leg(s, r) for r in L.ROLES)
    assert sc["status"] == "stopped" and sc["exit_reason"] == "stop"
    assert sc["exit_price"] >= sc["stop_level"] - 1e-9
    assert sc["stop_level"] == pytest.approx(1.5 * sc["entry_price"], abs=1e-4)
    # The put short was never stopped or moved, and settles.
    assert sp["status"] == "settled"
    assert sp["stop_level"] == pytest.approx(1.5 * sp["entry_price"], abs=1e-4)
    assert lc["status"] == "settled" and lp["status"] == "settled"   # wings never stopped
    assert s["status"] == "settled" and s["reconciliation"]["ok"]
    stop_fills = [f for f in s["fills"] if f["kind"] == "stop"]
    assert len(stop_fills) == 1 and stop_fills[0]["side"] == "BUY"


@pytest.mark.asyncio
async def test_gap_through_stop_fills_at_market():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    gap_at = at(D, 10, 30)
    feed.overrides[feed.sym(D, "C", 600)] = lambda t: (4.20, 4.32) if t >= gap_at else None
    s = await make(clock, feed).run_session(D)
    sc = leg(s, "short_call")
    assert sc["status"] == "stopped"
    assert sc["exit_price"] == pytest.approx(4.32)           # one tick, far above stop
    assert sc["exit_price"] > sc["stop_level"] * 1.5
    assert sc["stop_trigger_quote"] == [4.20, 4.32]
    assert datetime.fromisoformat(sc["stop_triggered_at"]) - gap_at < timedelta(seconds=3)
    assert leg(s, "short_put")["status"] == "settled"


@pytest.mark.asyncio
async def test_whipsaw_stops_both_shorts_independently():
    clock = SimClock(at(D, 9, 0))
    up = ramp(at(D, 10, 0), at(D, 11, 0), 600.0, 603.5)
    down = ramp(at(D, 11, 0), at(D, 13, 0), 603.5, 596.0)
    path = lambda t: up(t) if t < at(D, 11, 0) else down(t)  # noqa: E731
    s = await make(clock, FakeFeed(clock, path)).run_session(D)
    sc, sp = leg(s, "short_call"), leg(s, "short_put")
    assert sc["status"] == "stopped" and sp["status"] == "stopped"
    assert sc["exit_time"] < sp["exit_time"]
    assert leg(s, "long_call")["status"] == "settled"
    assert leg(s, "long_put")["status"] == "settled"
    assert s["reconciliation"]["ok"]


@pytest.mark.asyncio
async def test_close_before_settlement_flattens_at_1558():
    clock = SimClock(at(D, 9, 0))
    s = await make(clock, FakeFeed(clock, flat(600.4)),
                   replace(P, close_before_settlement=True)).run_session(D)
    shorts = [leg(s, r) for r in L.SHORT_ROLES]
    assert all(l["status"] == "closed" and l["exit_reason"] == "pre_settlement" for l in shorts)
    t = datetime.fromisoformat(shorts[0]["exit_time"])
    assert at(D, 15, 58) <= t < at(D, 16, 0)
    # Two minutes out the 10-point wings have no bid: they cannot be sold, so they are
    # left to cash-settle (worthless here) instead of being dumped at a fake price.
    for r in L.LONG_ROLES:
        assert leg(s, r)["exit_reason"] in ("pre_settlement", "settlement")
    assert s["status"] in ("closed", "settled") and s["reconciliation"]["ok"]


# --- skips / guards ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_0dte_expiry_skips():
    clock = SimClock(at(D, 9, 0))
    s = await make(clock, FakeFeed(clock, flat(), has_expiry=False)).run_session(D)
    assert s["status"] == "skipped" and "no 0DTE" in s["reason"]
    assert await trades() == []


@pytest.mark.asyncio
async def test_holiday_does_nothing():
    d = date(2026, 11, 26)
    clock = SimClock(at(d, 9, 0))
    assert await make(clock, FakeFeed(clock, flat())).run_session(d) is None
    assert await store.get_session(KEY, d) is None


@pytest.mark.asyncio
async def test_half_day_skips_for_too_little_time():
    d = date(2026, 11, 27)
    clock = SimClock(at(d, 9, 0))
    s = await make(clock, FakeFeed(clock, flat())).run_session(d)
    assert s["status"] == "skipped" and "early close" in s["reason"]


@pytest.mark.asyncio
async def test_half_day_allowed_when_configured_uses_1300_close():
    d = date(2026, 11, 27)
    clock = SimClock(at(d, 9, 0))
    s = await make(clock, FakeFeed(clock, flat()),
                   replace(P, min_minutes_to_close=60)).run_session(d)
    assert s["status"] == "settled"
    assert s["close_time"].astimezone(ET).time() == time(13, 0)


@pytest.mark.asyncio
async def test_stale_spot_through_grace_skips():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(), spot_stale_until=at(D, 12, 0))
    s = await make(clock, feed).run_session(D)
    assert s["status"] == "skipped"
    assert "entry window closed" in s["reason"] and "stale" in s["reason"]


@pytest.mark.asyncio
async def test_briefly_stale_spot_enters_within_grace():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(), spot_stale_until=at(D, 9, 45, 30))
    s = await make(clock, feed).run_session(D)
    assert s["status"] == "settled"
    assert at(D, 9, 45, 30) <= s["entry_time"] <= at(D, 9, 47)


@pytest.mark.asyncio
async def test_wide_leg_spread_skips():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    feed.overrides[feed.sym(D, "P", 590)] = lambda t: (0.05, 0.40)
    s = await make(clock, feed).run_session(D)
    assert s["status"] == "skipped" and "max_leg_spread" in s["reason"]


@pytest.mark.asyncio
async def test_no_capital_skips():
    clock = SimClock(at(D, 9, 0))
    s = await make(clock, FakeFeed(clock, flat()), capital=0).run_session(D)
    assert s["status"] == "skipped" and "sizing" in s["reason"]


@pytest.mark.asyncio
async def test_late_start_records_missed():
    clock = SimClock(at(D, 13, 0))
    s = await make(clock, FakeFeed(clock, flat())).run_session(D)
    assert s["status"] == "missed"
    assert await trades() == []


@pytest.mark.asyncio
async def test_safe_mode_blocks_entry():
    clock = SimClock(at(D, 9, 0))
    s = await make(clock, FakeFeed(clock, flat()), safe=True).run_session(D)
    assert s["status"] == "skipped" and "SAFE_MODE" in s["reason"]


# --- idempotency / restart / kill --------------------------------------------------
@pytest.mark.asyncio
async def test_never_double_enters_a_day():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat())
    s1 = await make(clock, feed).run_session(D)
    clock.t = at(D, 9, 30)                                  # same day, re-run
    s2 = await make(clock, feed).run_session(D)
    assert s2["id"] == s1["id"] and s2["status"] == "settled"
    assert len(entry_fills(s2)) == 4 and len(await trades()) == 4


async def _run_until(runner_coro, clock, until: datetime):
    task = asyncio.ensure_future(runner_coro)
    while clock.now() < until and not task.done():
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_restart_mid_position_recovers_legs_and_stops():
    clock = SimClock(at(D, 9, 0))
    path = ramp(at(D, 12, 0), at(D, 13, 0), 600.0, 603.5)
    feed = FakeFeed(clock, path)
    await _run_until(make(clock, feed).run_session(D), clock, at(D, 11, 0))
    mid = await store.get_session(KEY, D)
    assert mid["status"] == "open"
    stops_before = {l["role"]: l.get("stop_level") for l in mid["legs"]}
    # New process: fresh runner, feed and executor; same DB.
    feed2 = FakeFeed(clock, path)
    await feed2.contracts("XSP", D)                         # symbol table for the fake
    s = await make(clock, feed2).run_session(D)
    assert s["id"] == mid["id"]
    assert len(entry_fills(s)) == 4                         # no second entry
    assert {l["role"]: l.get("stop_level") for l in s["legs"]} == stops_before
    assert leg(s, "short_call")["status"] == "stopped"      # stop still enforced
    assert s["status"] == "settled" and s["reconciliation"]["ok"]


@pytest.mark.asyncio
async def test_crash_during_entry_does_not_reenter():
    clock = SimClock(at(D, 10, 0))
    await store.create_session(KEY, D, status="entering", mode="sim")
    s = await make(clock, FakeFeed(clock, flat())).run_session(D)
    assert s["status"] == "failed" and "interrupted during entry" in s["reason"]
    assert await trades() == []


@pytest.mark.asyncio
async def test_kill_switch_mid_position_stops_management():
    from app.engine.worker import close_open_trades
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat())
    await _run_until(make(clock, feed).run_session(D), clock, at(D, 11, 0))
    assert await close_open_trades() >= 4                   # what the kill route does
    assert all(t.status == "closed" for t in await trades())
    feed2 = FakeFeed(clock, flat())
    await feed2.contracts("XSP", D)
    s = await make(clock, feed2).run_session(D)
    assert s["status"] == "killed"
    assert all(l["status"] == "abandoned" for l in s["legs"])
    assert len(entry_fills(s)) == 4


@pytest.mark.asyncio
async def test_unsettled_prior_day_settles_on_next_start():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.2), settle_none=True)
    s = await make(clock, feed, replace(P, settlement_timeout_minutes=5)).run_session(D)
    assert s["status"] == "open" and "settlement price unavailable" in s["reason"]
    nxt = D + timedelta(days=1)
    clock.t = at(nxt, 8, 0)
    feed2 = FakeFeed(clock, flat(600.2))
    await feed2.contracts("XSP", D)
    await make(clock, feed2).recover_stale(nxt)
    s = await store.get_session(KEY, D)
    assert s["status"] == "settled" and s["settlement_price"] == pytest.approx(600.2)


# --- circuit breaker ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_loss_breach_halts_until_cleared():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    feed.overrides[feed.sym(D, "C", 600)] = lambda t: (14.8, 15.0) if t >= at(D, 10) else None
    alerts = []
    s = await make(clock, feed, alerts=alerts).run_session(D)
    assert s["daily_loss_breached"] is True
    assert leg(s, "short_put")["status"] == "settled"       # no flatten by default
    assert any(k == "xsp_fly_daily_loss" for k, _ in alerts)
    assert "max_daily_loss" in (await store.halt_reason())
    d2 = D + timedelta(days=1)
    clock.t = at(d2, 9, 0)
    s2 = await make(clock, FakeFeed(clock, flat())).run_session(d2)
    assert s2["status"] == "skipped" and "halted" in s2["reason"]
    assert await store.clear_halt() is True
    d3 = D + timedelta(days=2)
    clock.t = at(d3, 9, 0)
    s3 = await make(clock, FakeFeed(clock, flat())).run_session(d3)
    assert s3["status"] == "settled"


@pytest.mark.asyncio
async def test_daily_loss_breach_can_flatten():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    feed.overrides[feed.sym(D, "C", 600)] = lambda t: (14.8, 15.0) if t >= at(D, 10) else None
    s = await make(clock, feed, replace(P, flatten_on_daily_loss=True)).run_session(D)
    assert leg(s, "short_put")["exit_reason"] == "daily_loss"
    assert leg(s, "long_call")["exit_reason"] in ("daily_loss", "settlement")


# --- execution paths ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_combo_partial_fills_accumulate_to_target():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat())
    params = replace(P, max_contracts=2, max_daily_loss=2500)
    s = await make(clock, feed, params, capital=10_000,
                   executor=SimExecutor(feed, clock, max_fill_qty=1)).run_session(D)
    assert s["contracts"] == 2 and all(l["qty"] == 2 for l in s["legs"])
    assert len(entry_fills(s)) == 8
    assert s["reconciliation"]["ok"]


@pytest.mark.asyncio
async def test_leg_in_buys_wings_before_body():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat())
    s = await make(clock, feed, executor=SimExecutor(feed, clock, supports_combo=False)).run_session(D)
    order = [(f["role"], f["side"]) for f in entry_fills(s)]
    assert order[:2] == [("long_call", "BUY"), ("long_put", "BUY")]
    assert set(order[2:]) == {("short_call", "SELL"), ("short_put", "SELL")}
    assert s["status"] == "settled" and s["reconciliation"]["ok"]


class _NoPutBody(SimExecutor):
    """The short put never fills: the leg-in must unwind everything else."""

    async def single(self, leg, side, qty, limit, **kw):
        if leg["role"] == "short_put" and side == "SELL":
            return OrderResult("unfilled", detail="no fill")
        return await super().single(leg, side, qty, limit, **kw)


@pytest.mark.asyncio
async def test_leg_in_unwinds_cleanly_when_body_does_not_fill():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat())
    s = await make(clock, feed, executor=_NoPutBody(feed, clock, supports_combo=False)).run_session(D)
    assert s["status"] == "skipped" and s["contracts"] == 0
    net: dict[str, int] = {}
    for f in s["fills"]:
        net[f["symbol"]] = net.get(f["symbol"], 0) + (-1 if f["side"] == "SELL" else 1) * f["qty"]
    assert set(net.values()) == {0}                          # flat in every symbol
    kinds = [(f["role"], f["kind"]) for f in s["fills"] if f["kind"] != "entry"]
    # Excess short body is bought back BEFORE the wings are sold.
    assert kinds[0] == ("short_call", "unwind")
    assert {r for r, _ in kinds[1:]} == {"long_call", "long_put"}
    assert s["realized_pnl"] < 0 and await trades() == []


# --- several sessions --------------------------------------------------------------
@pytest.mark.asyncio
async def test_multi_session_paper_week_over_thanksgiving():
    """run_forever across Mon 11/23 .. Mon 11/30: three regular days, Thanksgiving
    (closed), the 13:00 half day (skipped by default), then Monday."""
    start = at(date(2026, 11, 23), 7, 0)
    paths = {
        date(2026, 11, 23): flat(600.2),
        date(2026, 11, 24): ramp(at(date(2026, 11, 24), 10), at(date(2026, 11, 24), 11),
                                 605.0, 609.0),
        date(2026, 11, 25): flat(598.4),
        date(2026, 11, 27): flat(601.0),
        date(2026, 11, 30): ramp(at(date(2026, 11, 30), 12), at(date(2026, 11, 30), 15),
                                 600.0, 594.0),
    }
    clock = SimClock(start)
    feed = FakeFeed(clock, lambda t: paths.get(t.date(), flat(600.0))(t))
    runner = make(clock, feed)
    await _run_until(runner.run_forever(), clock, at(date(2026, 12, 1), 8, 30))
    rows = {s["session_date"]: s for s in await store.recent_sessions(KEY, 20)}
    assert sorted(rows) == [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25),
                            date(2026, 11, 27), date(2026, 11, 30)]
    assert date(2026, 11, 26) not in rows
    assert rows[date(2026, 11, 27)]["status"] == "skipped"
    for d in (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 30)):
        assert rows[d]["status"] == "settled", (d, rows[d]["reason"])
        assert rows[d]["reconciliation"]["ok"], rows[d]["reconciliation"]
    assert leg(rows[date(2026, 11, 24)], "short_call")["status"] == "stopped"
    assert leg(rows[date(2026, 11, 30)], "short_put")["status"] == "stopped"
    trs = await trades()
    assert len(trs) == 16 and all(t.status == "closed" for t in trs)
    assert sum(t.pnl for t in trs) == pytest.approx(
        sum(rows[d]["realized_pnl"] for d in rows if rows[d]["realized_pnl"] is not None),
        abs=0.05)


# --- wiring ------------------------------------------------------------------------
def test_registered_activatable_in_sim_and_refused_live():
    from app.strategies import registry
    from app.strategies.xsp_iron_fly import XspIronFlyStrategy
    s = registry.get_strategy(KEY)
    assert isinstance(s, XspIronFlyStrategy) and s.ready and not s.external_runner
    assert XspIronFlyStrategy(IronFlyParams()).activation_block_reason() is None
    reason = XspIronFlyStrategy(IronFlyParams(mode="live")).activation_block_reason()
    assert reason and "no live XSP order route" in reason
    assert len(registry.list_strategies()) == 5


@pytest.mark.asyncio
async def test_worker_dispatches_run_live_first():
    from app.engine.worker import run_strategy
    from app.strategies.base import BaseStrategy

    class S(BaseStrategy):
        key = "x"
        ran = False

        async def scan(self, universe):
            raise AssertionError("generic loop must not run")

        async def run_live(self):
            S.ran = True

    await run_strategy(S())
    assert S.ran


def test_reconcile_flags_open_leg_and_unflat_ledger():
    s = {"mode": "sim", "fees": 0.0, "legs": [
        {"role": "short_call", "qty": 1, "status": "open", "trade_id": 1, "pnl": 0}],
        "fills": [{"symbol": "X", "side": "SELL", "qty": 1, "fee": 0}]}
    rec = reconcile_session(s, {1: {"status": "open", "pnl": None}})
    assert not rec["ok"] and len(rec["issues"]) == 3
    assert "sim mode has no broker statement" in rec["scope"]


# --- review fixes: stuck stop closes, stale quotes, interrupted entries, kill view ----
class _StuckCallStop(SimExecutor):
    """Nobody sells the short call back: its stop close can never fill."""

    async def single(self, leg, side, qty, limit, **kw):
        if leg["role"] == "short_call" and side == "BUY":
            return OrderResult("unfilled", detail="no liquidity")
        return await super().single(leg, side, qty, limit, **kw)


@pytest.mark.asyncio
async def test_unfilled_stop_close_does_not_blind_the_other_short():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, lambda t: 600.0 if t < at(D, 11) else ramp(
        at(D, 11), at(D, 13), 600.0, 596.0)(t))
    feed.overrides[feed.sym(D, "C", 600)] = lambda t: (4.20, 4.32) if t >= at(D, 10, 30) else None
    s = await make(clock, feed, executor=_StuckCallStop(feed, clock)).run_session(D)
    sc, sp = leg(s, "short_call"), leg(s, "short_put")
    # The call's stop fired but never filled: it stays on and settles at the close...
    assert sc["stop_triggered_at"] is not None and sc["exit_reason"] == "settlement"
    # ...while the put's stop was still watched and fired on the afternoon slide.
    assert sp["status"] == "stopped"
    assert datetime.fromisoformat(sp["exit_time"]) < at(D, 13, 30)
    assert s["status"] == "settled" and s["reconciliation"]["ok"]


@pytest.mark.asyncio
async def test_sim_executor_does_not_fill_a_stale_quote():
    clock = SimClock(at(D, 10, 0))
    feed = FakeFeed(clock, flat())
    await feed.contracts("XSP", D)
    sym = feed.sym(D, "C", 600)
    feed.overrides[sym] = lambda t: (1.00, 1.04, at(D, 9, 59, 20))     # 40 s old
    ex = SimExecutor(feed, clock)
    one = {"role": "short_call", "symbol": sym}
    res = await ex.single(one, "BUY", 1, 1.10, max_age=30.0)
    assert res.status == "unfilled" and "stale" in res.detail
    res = await ex.combo([one], 1, 0.5, max_age=30.0)
    assert res.status == "unfilled" and "stale" in res.detail
    assert (await ex.single(one, "BUY", 1, 1.10, max_age=60.0)).filled_qty == 1


@pytest.mark.asyncio
async def test_flatten_does_not_book_a_frozen_quote():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.4))
    frozen = at(D, 15, 50)
    feed.overrides[feed.sym(D, "C", 600)] = lambda t: (0.30, 0.34, frozen) if t >= frozen else None
    s = await make(clock, feed, replace(P, close_before_settlement=True)).run_session(D)
    sc, sp = leg(s, "short_call"), leg(s, "short_put")
    assert sp["exit_reason"] == "pre_settlement"              # fresh quote: flattened
    assert sc["exit_reason"] == "settlement"                  # frozen quote: not filled
    assert not [f for f in s["fills"] if f["role"] == "short_call" and f["kind"] != "entry"
                and f["kind"] != "settle"]
    assert s["reconciliation"]["ok"]


class _HangAfterFirstFill(SimExecutor):
    """Fills one combo then never answers again: a process killed mid-entry."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.combos = 0
        self.hung = asyncio.Event()

    async def _hang(self):
        self.hung.set()
        await asyncio.Event().wait()

    async def combo(self, legs, qty, limit_credit, **kw):
        if self.combos:
            await self._hang()
        res = await super().combo(legs, 1, limit_credit, **kw)
        self.combos += res.filled_qty
        return res

    async def single(self, leg, side, qty, limit, **kw):
        if side == "SELL" and leg["action"] == "SELL":   # the body: hang before it fills
            await self._hang()
        return await super().single(leg, side, qty, limit, **kw)


TWO = replace(P, max_contracts=2, max_daily_loss=5000, entry_reprice_seconds=1.0)


async def _interrupted_entry(clock, *, combo=True):
    feed = FakeFeed(clock, flat())
    ex = _HangAfterFirstFill(feed, clock, supports_combo=combo, max_fill_qty=1)
    task = asyncio.ensure_future(
        make(clock, feed, TWO, capital=10_000, executor=ex).run_session(D))
    await asyncio.wait_for(ex.hung.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s = await store.get_session(KEY, D)
    assert s["status"] == "entering"
    return s


@pytest.mark.asyncio
async def test_restart_mid_combo_entry_manages_the_filled_fly():
    clock = SimClock(at(D, 9, 0))
    s0 = await _interrupted_entry(clock)
    assert [l["qty"] for l in s0["legs"]] == [1, 1, 1, 1]
    feed2 = FakeFeed(clock, flat())
    await feed2.contracts("XSP", D)
    s = await make(clock, feed2, TWO, capital=10_000).run_session(D)
    assert s["id"] == s0["id"] and len(entry_fills(s)) == 4   # resumed, never re-entered
    assert s["contracts"] == 1 and s["net_credit"] > 0
    assert all(l["stop_level"] for l in s["legs"] if l["action"] == "SELL")
    assert s["status"] == "settled" and s["reconciliation"]["ok"]
    assert len(await trades()) == 4


@pytest.mark.asyncio
async def test_restart_mid_leg_in_unwinds_unmatched_wings():
    clock = SimClock(at(D, 9, 0))
    s0 = await _interrupted_entry(clock, combo=False)
    assert {l["role"]: l["qty"] for l in s0["legs"]} == {
        "short_call": 0, "short_put": 0, "long_call": 2, "long_put": 2}
    feed2 = FakeFeed(clock, flat())
    await feed2.contracts("XSP", D)
    s = await make(clock, feed2, TWO, capital=10_000).run_session(D)
    assert s["status"] == "failed" and "unmatched fills unwound" in s["reason"]
    net: dict[str, int] = {}
    for f in s["fills"]:
        net[f["symbol"]] = net.get(f["symbol"], 0) + (-1 if f["side"] == "SELL" else 1) * f["qty"]
    assert set(net.values()) == {0} and await trades() == []


@pytest.mark.asyncio
async def test_entry_interrupted_on_a_prior_day_settles_its_fills():
    clock = SimClock(at(D, 9, 0))
    await _interrupted_entry(clock)
    nxt = D + timedelta(days=1)
    clock.t = at(nxt, 8, 0)
    feed2 = FakeFeed(clock, flat(600.2))
    await feed2.contracts("XSP", D)
    await make(clock, feed2, TWO, capital=10_000).recover_stale(nxt)
    s = await store.get_session(KEY, D)
    assert s["status"] == "settled" and s["settlement_price"] == pytest.approx(600.2)
    assert s["reconciliation"]["ok"] and len(await trades()) == 4


@pytest.mark.asyncio
async def test_status_view_shows_killed_right_after_the_kill_switch():
    from app.engine.worker import close_open_trades
    from app.strategies.xsp_iron_fly import XspIronFlyStrategy
    clock = SimClock(at(D, 9, 0))
    await _run_until(make(clock, FakeFeed(clock, flat())).run_session(D), clock, at(D, 11))
    view = await XspIronFlyStrategy(IronFlyParams()).status_view()
    assert view["sessions"][0]["status"] == "open"
    await close_open_trades()
    view = await XspIronFlyStrategy(IronFlyParams()).status_view()
    assert view["sessions"][0]["status"] == "killed"
