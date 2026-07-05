from app.strategies.triple_calendar.params import TripleCalendarParams


def test_defaults():
    p = TripleCalendarParams()
    assert p.underlying == "QQQ" and p.sell_dte == 21 and p.buy_dte == 28
    assert p.strike_round == 5.0 and p.em_buffer == 5.0 and p.manual_nudge == 0.0
    assert p.profit_target == 0.10 and p.stop_loss_pct == 0.25
    assert p.vix_threshold == 13.0 and p.vix_scale == 0.5
    assert p.vix_entry_max == 16.0
    assert p.roll_on_exit is True
    assert p.max_calendars == 4 and p.allow_fifth is False
    assert p.event_dates == []
