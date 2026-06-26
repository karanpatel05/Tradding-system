from datetime import date

from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.live import entry_legs, exit_legs

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


def _pos():
    return Position("QQQ", date(2026, 6, 25),
                    [Calendar(430.0, SE, BE), Calendar(445.0, SE, BE), Calendar(460.0, SE, BE)],
                    net_debit=3.0)


def test_entry_legs_six_legs_correct_actions():
    legs = entry_legs(_pos())
    assert len(legs) == 6
    sells = [x for x in legs if x["action"] == "SELL"]
    buys = [x for x in legs if x["action"] == "BUY"]
    assert len(sells) == 3 and len(buys) == 3
    assert all(x["expiry"] == SE for x in sells) and all(x["expiry"] == BE for x in buys)
    assert {x["strike"] for x in sells} == {430.0, 445.0, 460.0}
    assert all(x["right"] == "C" for x in legs)


def test_exit_legs_reverse_actions():
    legs = exit_legs(_pos())
    assert {x["action"] for x in legs if x["expiry"] == SE} == {"BUY"}   # buy back the short
    assert {x["action"] for x in legs if x["expiry"] == BE} == {"SELL"}  # sell the long
