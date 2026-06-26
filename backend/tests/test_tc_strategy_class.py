from app.strategies.triple_calendar.strategy_class import TripleCalendarStrategy


def test_strategy_metadata():
    s = TripleCalendarStrategy()
    assert s.ready is True and s.is_options is True
    assert "Calendar" in s.name
    ov = s.overview()
    assert ov["ready"] is True and ov["rules"]


def test_registered_in_registry():
    from app.strategies.registry import get_strategy
    s = get_strategy("strategy_2")
    assert s is not None and getattr(s, "is_options", False) is True
