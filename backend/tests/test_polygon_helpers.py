from datetime import date

import pytest

from app.options.base import OptionContract, PolygonNotAuthorized
from app.options.polygon import (_agg_path, _check_status, _normalize_bar,
                                 _normalize_contract, _tf)


def test_tf_map():
    assert _tf("1m") == (1, "minute")
    assert _tf("1d") == (1, "day")
    with pytest.raises(ValueError):
        _tf("2w")


def test_agg_path():
    p = _agg_path("O:SPY260622C00747000", "1d", date(2024, 1, 1), date(2024, 6, 30))
    assert p == "/v2/aggs/ticker/O:SPY260622C00747000/range/1/day/2024-01-01/2024-06-30"


def test_normalize_bar():
    r = {"t": 1718971800000, "o": 1.0, "h": 1.5, "l": 0.9, "c": 1.2, "v": 345}
    assert _normalize_bar(r, "O:SPY260622C00747000") == {
        "time": 1718971800000, "open": 1.0, "high": 1.5, "low": 0.9,
        "close": 1.2, "volume": 345.0, "symbol": "O:SPY260622C00747000"}


def test_normalize_contract():
    r = {"ticker": "O:SPY260622C00747000", "underlying_ticker": "SPY",
         "expiration_date": "2026-06-22", "strike_price": 747.0,
         "contract_type": "call", "shares_per_contract": 100}
    c = _normalize_contract(r)
    assert isinstance(c, OptionContract) and c.right == "C" and c.underlying == "SPY"


def test_check_status_403_not_authorized():
    with pytest.raises(PolygonNotAuthorized):
        _check_status(403, {"status": "NOT_AUTHORIZED", "message": "upgrade plan"})
    _check_status(200, {"status": "OK"})  # no raise
