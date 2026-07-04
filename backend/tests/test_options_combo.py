from datetime import date

from app.options.combo import (combo_value, curve_stats, exit_check, payoff_at,
                               payoff_curve)

NEAR = "2026-07-24"
FAR = "2026-07-31"


def leg(action, right, strike, expiry, qty=1):
    return {"action": action, "right": right, "strike": strike, "expiry": expiry, "qty": qty}


def test_combo_value_signs_and_missing():
    legs = [leg("BUY", "C", 100, FAR), leg("SELL", "C", 100, NEAR)]
    assert combo_value(legs, [3.0, 1.0]) == 2.0     # +3 -1
    assert combo_value(legs, [3.0, None]) is None    # missing mid


def test_vertical_payoff_is_clean_intrinsic_at_expiry():
    # Bull call debit spread, both legs expire NEAR.
    legs = [leg("BUY", "C", 100, NEAR), leg("SELL", "C", 105, NEAR)]
    ev = date.fromisoformat(NEAR)
    # At expiry the far leg (BS) collapses to intrinsic; check max value == width.
    assert abs(payoff_at(legs, 120, ev, 0.25, 0.04) - 5.0) < 1e-6   # 20 - 15
    assert abs(payoff_at(legs, 90, ev, 0.25, 0.04) - 0.0) < 1e-6


def test_calendar_has_time_value_at_near_expiry():
    # Long far call, short near call, same strike → calendar. At the NEAR expiry the short
    # leg is worthless ATM but the long (far) leg still holds BS time value → positive tent.
    legs = [leg("BUY", "C", 100, FAR), leg("SELL", "C", 100, NEAR)]
    ev = date.fromisoformat(NEAR)
    assert payoff_at(legs, 100, ev, 0.25, 0.04) > 0.0


def test_curve_and_stats():
    legs = [leg("BUY", "C", 100, NEAR), leg("SELL", "C", 105, NEAR)]
    curve = payoff_curve(legs, net_debit=2.0, eval_date=date.fromisoformat(NEAR),
                         iv=0.25, r=0.04, lo=90, hi=115, points=51)
    assert curve[0]["pnl"] < 0 < curve[-1]["pnl"]     # loss below, profit above
    stats = curve_stats(curve)
    assert stats["max_profit"] > 0 and stats["max_loss"] < 0
    assert stats["breakevens"] == [102.0]   # exactly one breakeven at 100 + $2 debit


def test_exit_check_debit_only():
    assert exit_check(2.0, 2.2, 0.10, 0.25) == "target"   # +10%
    assert exit_check(2.0, 1.5, 0.10, 0.25) == "stop"     # -25%
    assert exit_check(2.0, 2.1, 0.10, 0.25) is None
    assert exit_check(-2.0, 5.0, 0.10, 0.25) is None      # credit: no %-exit
    assert exit_check(2.0, None, 0.10, 0.25) is None
