from datetime import date

from app.strategies.triple_calendar.legs import (atm_strike, expected_move,
                                                 nearest_friday, outer_strikes, snap, target_expiry)


def test_nearest_friday():
    assert nearest_friday(date(2026, 6, 24)) == date(2026, 6, 26)   # Wed -> Fri (+2)
    assert nearest_friday(date(2026, 6, 26)) == date(2026, 6, 26)   # Fri -> same


def test_target_expiry_lands_on_friday():
    exp = target_expiry(date(2026, 6, 25), 21)
    assert exp.weekday() == 4 and abs((exp - date(2026, 6, 25)).days - 21) <= 3


def test_atm_and_outer_strikes():
    assert atm_strike(443.2, 5.0) == 445.0
    assert expected_move(6.0, 5.5) == 11.5
    lo, up = outer_strikes(445.0, 11.5, 5.0, 5.0, nudge=0.0)
    assert up == 460.0 and lo == 430.0          # 445 ± round(16.5/5)*5 = ±15
    lo2, up2 = outer_strikes(445.0, 11.5, 5.0, 5.0, nudge=5.0)
    assert up2 == 465.0 and lo2 == 425.0        # widened by the nudge


def test_snap_to_available_strikes():
    assert snap(447.0, [440.0, 445.0, 450.0]) == 445.0
