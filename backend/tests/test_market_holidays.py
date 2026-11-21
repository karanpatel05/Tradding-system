"""NYSE full-day closures are known for every year the calendar claims to cover
(cross-checked against pandas_market_calendars when this was written)."""
from datetime import date

import pytest

from app.services import market

NYSE_2027 = [date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
             date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
             date(2027, 11, 25), date(2027, 12, 24)]


@pytest.mark.parametrize("d", NYSE_2027)
def test_2027_holidays_are_closed(d):
    assert not market._is_trading_day(d)


def test_ordinary_2027_weekday_is_open():
    assert market._is_trading_day(date(2027, 1, 4))


def test_uncovered_year_warns_once(caplog):
    market._warned_years.discard(2031)
    with caplog.at_level("WARNING", logger="trading.market"):
        market._is_trading_day(date(2031, 3, 3))
        market._is_trading_day(date(2031, 3, 4))
    assert sum("2031" in r.getMessage() for r in caplog.records) == 1
