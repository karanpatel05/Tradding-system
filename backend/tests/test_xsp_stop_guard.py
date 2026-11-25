"""XSP stop trigger and stop-close pricing must ignore bad quotes (2026-11-25: a
bid 4.52 / ask 9.99 stub fired the short-call stop 3 s after entry, and the close was
priced at that ask + 0.05) while a REAL gap through the stop still fires promptly."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.strategies.xsp_iron_fly import logic as L
from app.strategies.xsp_iron_fly.feed import Quote
from tests.test_xsp_fly_sim import _db  # noqa: F401  (autouse: fresh tables per test)
from tests.test_xsp_fly_sim import D, P, FakeFeed, SimClock, at, flat, leg, make

TODAY_STOP = 7.875                       # 1.5 x the 5.25 short-call entry on 2026-11-25
GUARD = dict(max_age=3.0, max_spread_abs=0.50, max_spread_pct=0.25)


# --- pure trigger rule ------------------------------------------------------------------
def test_todays_recorded_quote_does_not_fire():
    ok, why = L.stop_check(4.52, 9.99, 0.5, TODAY_STOP, "ask", **GUARD)
    assert ok is False                    # too wide for the ask, and its mid 7.255 < stop


def test_a_tight_quote_past_the_stop_fires():
    assert L.stop_check(7.90, 8.00, 0.5, TODAY_STOP, "ask", **GUARD)[0] is True


def test_a_real_gap_with_a_wide_spread_still_fires_on_the_mid():
    ok, _ = L.stop_check(8.00, 11.00, 0.5, TODAY_STOP, "ask", **GUARD)   # mid 9.50
    assert ok is True


def test_stale_and_one_sided_quotes_never_fire():
    assert L.stop_check(9.00, 9.10, 10.0, TODAY_STOP, "ask", **GUARD) == (False, "stale")
    assert L.stop_check(9.00, 9.10, None, TODAY_STOP, "ask", **GUARD) == (False, "stale")
    assert L.stop_check(0.0, 9.99, 0.5, TODAY_STOP, "ask", **GUARD) == (False, "one-sided")
    assert L.stop_check(None, 9.99, 0.5, TODAY_STOP, "ask", **GUARD) == (False, "one-sided")


def test_mark_trigger_uses_the_mid():
    assert L.stop_check(7.50, 8.10, 0.5, TODAY_STOP, "mark", **GUARD)[0] is False  # mid 7.80
    assert L.stop_check(7.80, 8.00, 0.5, TODAY_STOP, "mark", **GUARD)[0] is True   # mid 7.90


# --- pure close pricing -----------------------------------------------------------------
def test_stop_close_limit_caps_then_steps_up_to_the_touch():
    # today's stub: mid 7.255, cap max(0.30, 0.7255) = 0.7255
    lims = [L.stop_close_limit(4.52, 9.99, through=0.05, tick=0.05, attempt=k)
            for k in range(6)]
    assert lims[0] == pytest.approx(8.00)          # mid + cap, rounded up to the tick
    assert lims == sorted(lims) and lims[-1] == pytest.approx(10.05)   # never beyond
    assert all(lim <= 10.05 + 1e-9 for lim in lims)


def test_stop_close_limit_on_a_tight_quote_is_the_touch():
    assert L.stop_close_limit(4.20, 4.32, through=0.05, tick=0.05,
                              attempt=0) == pytest.approx(4.40)


# --- end to end through the runner (sim harness) ------------------------------------------
class WideStubFeed(FakeFeed):
    """Between t0 and t1 the short call quotes a stub: bid 0.6 x mid, ask 2.2 x mid. Its
    ask is far past a 1.5 x entry stop but its mid (1.4 x) is not."""

    def __init__(self, *a, t0, t1, ts_shift=0.0, **k):
        super().__init__(*a, **k)
        self.t0, self.t1, self.ts_shift = t0, t1, ts_shift

    def quote(self, symbol):
        q = super().quote(symbol)
        now = self.clock.now()
        if (q is not None and symbol == self.sym(D, "C", 600)
                and self.t0 <= now < self.t1):
            m = (q.bid + q.ask) / 2
            return Quote(round(0.6 * m, 2), round(2.2 * m, 2),
                         now - timedelta(seconds=self.ts_shift))
        return q


@pytest.mark.asyncio
async def test_a_wide_stub_quote_after_entry_does_not_stop_the_short():
    clock = SimClock(at(D, 9, 0))
    feed = WideStubFeed(clock, flat(600.0), t0=at(D, 9, 46), t1=at(D, 10, 30))
    s = await make(clock, feed).run_session(D)
    sc = leg(s, "short_call")
    assert sc["status"] == "settled" and sc.get("stop_triggered_at") is None


@pytest.mark.asyncio
async def test_a_stale_quote_past_the_stop_does_not_fire():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    feed.overrides[feed.sym(D, "C", 600)] = (
        lambda t: (4.20, 4.32, t - timedelta(seconds=20)) if t >= at(D, 10, 30) else None)
    s = await make(clock, feed).run_session(D)
    assert leg(s, "short_call").get("stop_triggered_at") is None


@pytest.mark.asyncio
async def test_a_single_spike_is_not_confirmed():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    spike = at(D, 10, 30)
    feed.overrides[feed.sym(D, "C", 600)] = (
        lambda t: (4.20, 4.32) if spike <= t < spike + timedelta(seconds=0.5) else None)
    s = await make(clock, feed).run_session(D)
    assert leg(s, "short_call").get("stop_triggered_at") is None


@pytest.mark.asyncio
async def test_a_real_gap_still_fires_within_a_few_seconds():
    clock = SimClock(at(D, 9, 0))
    feed = FakeFeed(clock, flat(600.0))
    gap_at = at(D, 10, 30)
    feed.overrides[feed.sym(D, "C", 600)] = lambda t: (4.20, 4.32) if t >= gap_at else None
    s = await make(clock, feed).run_session(D)
    sc = leg(s, "short_call")
    assert sc["status"] == "stopped"
    fired = datetime.fromisoformat(sc["stop_triggered_at"])
    assert fired - gap_at <= timedelta(seconds=P.monitor_interval + 1.5)
