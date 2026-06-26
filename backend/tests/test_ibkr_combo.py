from datetime import date

from app.ibkr.contracts import combo, option


def test_option_contract():
    o = option("QQQ", date(2026, 7, 17), 445.0, "C")
    assert o.symbol == "QQQ" and o.secType == "OPT"
    assert o.lastTradeDateOrContractMonth == "20260717"
    assert o.strike == 445.0 and o.right == "C" and o.exchange == "SMART"


def test_combo_bag_legs():
    bag = combo("QQQ", [(111, "SELL"), (222, "BUY")])
    assert bag.secType == "BAG" and bag.symbol == "QQQ" and bag.currency == "USD"
    assert len(bag.comboLegs) == 2
    assert bag.comboLegs[0].conId == 111 and bag.comboLegs[0].action == "SELL"
    assert bag.comboLegs[1].conId == 222 and bag.comboLegs[1].ratio == 1
