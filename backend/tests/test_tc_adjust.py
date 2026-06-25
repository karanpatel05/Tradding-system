from datetime import date

from app.strategies.triple_calendar.legs import Calendar, Position, add_calendar, breached
from app.strategies.triple_calendar.params import TripleCalendarParams

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


def _pos():
    return Position(underlying="QQQ", entry_date=date(2026, 6, 25), calendars=[
        Calendar(430.0, SE, BE), Calendar(445.0, SE, BE), Calendar(460.0, SE, BE)])


def test_breached_sides():
    assert breached(_pos(), 470.0) == "upper"
    assert breached(_pos(), 420.0) == "lower"
    assert breached(_pos(), 445.0) is None


def test_add_calendar_extends_and_caps():
    p = TripleCalendarParams()  # max 4, allow_fifth False
    pos = _pos()
    c = add_calendar(pos, "upper", SE, BE, p)
    assert c is not None and c.strike == 475.0 and len(pos.calendars) == 4  # 460 + spacing 15
    assert add_calendar(pos, "upper", SE, BE, p) is None                    # capped at 4


def test_fifth_allowed_when_flagged():
    p = TripleCalendarParams(allow_fifth=True)
    pos = _pos()
    add_calendar(pos, "upper", SE, BE, p)        # 4th
    c = add_calendar(pos, "lower", SE, BE, p)    # 5th allowed
    assert c is not None and len(pos.calendars) == 5
