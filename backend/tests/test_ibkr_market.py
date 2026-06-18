import math

from app.ibkr.market import normalize_ticker


class _T:
    def __init__(self, bid, ask, last, close):
        self.bid, self.ask, self.last, self.close = bid, ask, last, close


def test_normalize_prefers_last_then_mid_then_close():
    q = normalize_ticker(_T(10.0, 10.2, 10.1, 9.9))
    assert q["last"] == 10.1 and q["bid"] == 10.0 and q["ask"] == 10.2
    assert q["price"] == 10.1                       # last wins
    # NaN last -> mid of bid/ask
    q2 = normalize_ticker(_T(10.0, 10.2, math.nan, 9.9))
    assert q2["price"] == 10.1
    # no last/quote -> close
    q3 = normalize_ticker(_T(math.nan, math.nan, math.nan, 9.9))
    assert q3["price"] == 9.9


def test_nan_fields_become_none():
    q = normalize_ticker(_T(math.nan, math.nan, math.nan, math.nan))
    assert q["bid"] is None and q["ask"] is None and q["last"] is None
    assert q["close"] is None and q["price"] is None
