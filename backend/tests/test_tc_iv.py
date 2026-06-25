from app.strategies.triple_calendar.iv import bs_price, implied_vol


def test_bs_price_atm_call_put_positive():
    c = bs_price("C", 100, 100, 0.25, 0.04, 0.20)
    p = bs_price("P", 100, 100, 0.25, 0.04, 0.20)
    assert c > 0 and p > 0


def test_implied_vol_round_trip():
    price = bs_price("C", 100, 100, 0.25, 0.04, 0.20)
    iv = implied_vol(price, "C", 100, 100, 0.25, 0.04)
    assert iv is not None and abs(iv - 0.20) < 1e-3


def test_implied_vol_none_on_nonpositive():
    assert implied_vol(0.0, "C", 100, 100, 0.25, 0.04) is None
    assert implied_vol(5.0, "C", 100, 100, 0.0, 0.04) is None  # no time
