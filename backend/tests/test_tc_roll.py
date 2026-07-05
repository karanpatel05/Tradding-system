"""Triple Calendar roll-on-exit: manage() labels +10%/-25% as 'roll' when roll_on_exit is on;
manage_step closes then reopens at the current level (fail-closed on VIX).
   Run: python -m tests.test_tc_roll
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.strategy import manage

DAY = date(2026, 1, 1)
SELL = DAY + timedelta(days=21)
BUY = DAY + timedelta(days=28)


class _Src:
    """Calendar value = buy - sell = `value`; underlying_close None to skip breach."""
    def __init__(self, value):
        self.value = value
    def option_close(self, u, expiry, strike, right, d):
        return self.value if expiry == BUY else 0.0
    def underlying_close(self, u, d):
        return None


def _pos():
    return Position(underlying="QQQ", entry_date=DAY,
                    calendars=[Calendar(725.0, SELL, BUY)], net_debit=10.0)


def test_manage_rolls_on_target_and_stop_when_enabled():
    p = TripleCalendarParams()  # roll_on_exit defaults True
    act, info = manage(_pos(), DAY, _Src(11.0), p)      # +10%
    assert act == "roll" and info["reason"] == "target"
    act, info = manage(_pos(), DAY, _Src(7.5), p)       # -25%
    assert act == "roll" and info["reason"] == "stop_loss"


def test_manage_exits_when_roll_disabled():
    p = TripleCalendarParams(roll_on_exit=False)
    act, info = manage(_pos(), DAY, _Src(11.0), p)
    assert act == "exit" and info["reason"] == "target"
    act, info = manage(_pos(), DAY, _Src(7.5), p)
    assert act == "exit" and info["reason"] == "stop_loss"


class _EntrySrc:
    """Supports decide_entry (spot/chain/option_close) AND management valuation. The open
    calendar is worth `value`; fresh entries build from the same rising-with-DTE curve."""
    def __init__(self, value, spot=445.0):
        self.value, self.spot = value, spot
        self.strikes = [float(r) for r in range(400, 491, 5)]
    def underlying_close(self, u, d):
        return self.spot
    def chain_strikes(self, u, e):
        return self.strikes
    def option_close(self, u, expiry, strike, right, d):
        # The seeded position's strike (725) values cleanly: buy leg -> `value`, sell leg -> 0,
        # so its calendar value == `value`. Fresh decide_entry uses strikes near ATM (~445),
        # which fall to the rising-with-DTE curve -> a positive-debit structure.
        if strike == 725.0:
            return self.value if expiry == BUY else 0.0
        return 1.0 + 0.1 * max((expiry - d).days, 0)


class _Exec:
    def __init__(self):
        self.calls = []
    async def __call__(self, underlying, legs, **kw):
        self.calls.append((kw.get("reason"), kw.get("action"), len(legs)))
        return {"transmitted": False, "paper": True, "order_id": None}


@pytest.mark.asyncio
async def test_manage_step_roll_reopens_when_vix_ok():
    from app.strategies.triple_calendar.live import manage_step
    ex = _Exec()
    p = TripleCalendarParams()
    out = await manage_step(DAY, _EntrySrc(7.5), p, ex, position=_pos(), vix=12.0)  # -25% stop
    assert out["action"] == "roll" and out["info"]["reopened"] is True
    assert out["position"] is not None
    assert out["info"]["closed_pnl"] == pytest.approx(-2.5)   # 7.5 - 10.0
    reasons = [c[0] for c in ex.calls]
    assert "roll_stop_loss" in reasons and "roll_entry" in reasons   # a SELL then a BUY


@pytest.mark.asyncio
async def test_manage_step_roll_target_reopens():
    from app.strategies.triple_calendar.live import manage_step
    ex = _Exec()
    out = await manage_step(DAY, _EntrySrc(11.0), TripleCalendarParams(), ex,
                            position=_pos(), vix=12.0)  # +10% target
    assert out["action"] == "roll" and out["info"]["reason"] == "target"
    assert out["info"]["closed_pnl"] == pytest.approx(1.0) and out["info"]["reopened"] is True


@pytest.mark.asyncio
async def test_manage_step_roll_fail_closed_no_reopen():
    from app.strategies.triple_calendar.live import manage_step
    # vix=None (unreadable) and vix>16 must both close WITHOUT reopening.
    for vix in (None, 17.0):
        ex = _Exec()
        out = await manage_step(DAY, _EntrySrc(7.5), TripleCalendarParams(), ex,
                                position=_pos(), vix=vix)
        assert out["action"] == "roll" and out["position"] is None
        assert out["info"]["reopened"] is False
        actions = [c[1] for c in ex.calls]
        assert actions == ["SELL"], f"expected close-only, got {ex.calls} (vix={vix})"


@pytest.mark.asyncio
async def test_holding_tick_snapshot_can_reopen():
    # Regression: a roll fires on a HOLDING tick; _resolve_snapshot must prefetch the fresh
    # entry strikes so decide_entry can price a new position from that same snapshot.
    from datetime import date
    from app.strategies.triple_calendar import strategy_class
    from app.strategies.triple_calendar.strategy import decide_entry

    today = date(2026, 1, 5)
    strikes = [float(r) for r in range(400, 491, 5)]
    p = TripleCalendarParams()

    class Src:
        async def aoption_mid(self, u, expiry, strike, right):
            return 1.0 + 0.1 * max((expiry - today).days, 0)  # positive, rises with DTE

    held = Position("QQQ", today, [Calendar(445.0, date(2026, 1, 30), date(2026, 2, 6))],
                    net_debit=2.0)
    snap = await strategy_class._resolve_snapshot(Src(), p, 445.0, strikes, today, position=held)
    new_pos = decide_entry(today, snap, p, events=[], vix=12.0)
    assert new_pos is not None and len(new_pos.calendars) == 3, "reopen must work from a holding-tick snapshot"
