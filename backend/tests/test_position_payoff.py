from app.options.position_payoff import legs_from_calendars


def test_legs_from_calendars_builds_sell_near_buy_far_calls():
    cals = [
        {"strike": 470.0, "sell": "2026-08-15", "buy": "2026-09-19"},
        {"strike": 480.0, "sell": "2026-08-15", "buy": "2026-09-19"},
    ]
    legs = legs_from_calendars(cals)
    assert legs == [
        {"action": "SELL", "right": "C", "strike": 470.0, "expiry": "2026-08-15", "qty": 1},
        {"action": "BUY",  "right": "C", "strike": 470.0, "expiry": "2026-09-19", "qty": 1},
        {"action": "SELL", "right": "C", "strike": 480.0, "expiry": "2026-08-15", "qty": 1},
        {"action": "BUY",  "right": "C", "strike": 480.0, "expiry": "2026-09-19", "qty": 1},
    ]


def test_legs_from_calendars_empty():
    assert legs_from_calendars([]) == []
    assert legs_from_calendars(None) == []
