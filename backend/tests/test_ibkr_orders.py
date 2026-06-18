from app.engine.ibkr_orders import build_bracket, intent_to_action


def test_intent_to_action():
    assert intent_to_action("enter_long") == "BUY"
    assert intent_to_action("exit_short") == "BUY"
    assert intent_to_action("exit_long") == "SELL"
    assert intent_to_action("enter_short") == "SELL"


def test_unknown_intent_raises():
    import pytest
    with pytest.raises(ValueError):
        intent_to_action("nonsense")


def test_build_bracket_long_market_parent_oco_children():
    parent, tp, stop = build_bracket("long", 10, target=110.0, stop=95.0,
                                     parent_id=1, tp_id=2, stop_id=3)
    assert parent.action == "BUY" and parent.orderType == "MKT" and parent.totalQuantity == 10
    assert parent.transmit is False
    assert tp.action == "SELL" and tp.orderType == "LMT" and tp.lmtPrice == 110.0
    assert tp.parentId == 1 and tp.transmit is False
    assert stop.action == "SELL" and stop.orderType == "STP" and stop.auxPrice == 95.0
    assert stop.parentId == 1 and stop.transmit is True   # last leg transmits


def test_build_bracket_short_reverses_actions():
    parent, tp, stop = build_bracket("short", 4, target=90.0, stop=105.0)
    assert parent.action == "SELL"
    assert tp.action == "BUY" and stop.action == "BUY"


def test_build_bracket_unknown_side_raises():
    import pytest
    with pytest.raises(ValueError):
        build_bracket("sideways", 1, target=1.0, stop=1.0)
