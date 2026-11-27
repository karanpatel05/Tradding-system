from datetime import date, timedelta

import pytest

from app.strategies.xsp_iron_fly import calendar


@pytest.mark.parametrize(("year", "sessions", "half_days"), [
    (2024, 252, ["07-03", "11-29", "12-24"]),
    (2025, 250, ["07-03", "11-28", "12-24"]),
])
def test_historical_session_counts_and_early_closes(year, sessions, half_days):
    first = date(year, 1, 1)
    days = (first + timedelta(days=n) for n in range((date(year + 1, 1, 1) - first).days))
    closes = [close for day in days if (close := calendar.close_time(day))]
    assert len(closes) == sessions
    assert [c.strftime("%m-%d") for c in closes if c.hour == 13] == half_days


def test_carter_national_day_of_mourning():
    assert calendar.close_time(date(2025, 1, 9)) is None
    assert calendar.is_trading_day(date(2025, 1, 8))
    assert calendar.is_trading_day(date(2025, 1, 10))
