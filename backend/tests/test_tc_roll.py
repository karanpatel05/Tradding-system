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
