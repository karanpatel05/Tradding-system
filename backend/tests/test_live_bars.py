from datetime import date, datetime

from app.config import get_settings
from app.history.live_bars import ET, Trade, fold, is_rth_trade, minute_of, stock_symbols

HOLIDAY = date(2026, 11, 26)
TRADING_DAY = date(2026, 11, 24)


def _et(h, m, s=0, ms=0, day=TRADING_DAY):
    dt = datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET)
    return int(dt.timestamp()) * 1000 + ms


def T(ts, seq, price, size, symbol="SPY", valid=True):
    return Trade(symbol, ts, seq, price, size, valid)


def test_stock_symbols_from_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "live_bar_symbols", "spy, QQQ,SPX,qqq,bad sym,NVDA")
    assert stock_symbols() == ("SPY", "QQQ", "NVDA")


def test_rth_window():
    assert is_rth_trade(_et(9, 30, 0, 0), False)
    assert not is_rth_trade(_et(9, 29, 59, 999), False)
    assert is_rth_trade(_et(16, 0, 0, 0), False)          # closing print kept
    assert not is_rth_trade(_et(16, 0, 0, 1), False)
    assert not is_rth_trade(_et(10, 0, 0, 0), True)             # extended-hours flag
    assert is_rth_trade(_et(10, 0, 0, 0), False)                # session filter only
    assert not is_rth_trade(_et(10, 0, 0, 0, day=HOLIDAY), False)


def test_closing_print_folds_into_1559():
    assert minute_of(_et(16, 0, 0, 0)) == _et(15, 59, 0, 0)


def test_fold_builds_ohlcv():
    bars = {}
    fold(bars, [T(_et(10, 0, 1), 1, 100.0, 10), T(_et(10, 0, 30), 2, 101.0, 5),
                T(_et(10, 0, 59), 3, 99.5, 1)])
    assert bars[_et(10, 0)] == {"time": _et(10, 0), "open": 100.0, "high": 101.0, "low": 99.5,
                                "close": 99.5, "volume": 16.0, "trades": 3}


def test_fold_odd_lots_add_volume_not_price():
    bars = {}
    fold(bars, [T(_et(10, 0, 1), 1, 100.0, 100), T(_et(10, 0, 2), 2, 105.0, 5, valid=False),
                T(_et(10, 0, 3), 3, 101.0, 100)])
    b = bars[_et(10, 0)]
    assert (b["high"], b["close"], b["volume"], b["trades"]) == (101.0, 101.0, 205.0, 3)


def test_fold_late_trade_updates_its_own_minute():
    bars = {}
    fold(bars, [T(_et(10, 1, 5), 2, 101.0, 1)])
    touched = fold(bars, [T(_et(10, 0, 50), 1, 100.0, 2)])
    assert touched == {_et(10, 0)} and bars[_et(10, 1)]["volume"] == 1.0 \
        and bars[_et(10, 0)]["close"] == 100.0


def test_fold_invalid_only_minute_then_first_valid_resets_prices():
    bars = {}
    fold(bars, [T(_et(10, 0, 1), 1, 105.0, 5, valid=False)])
    assert bars[_et(10, 0)]["close"] == 105.0
    fold(bars, [T(_et(10, 0, 2), 2, 100.0, 10)])
    b = bars[_et(10, 0)]
    assert (b["open"], b["high"], b["low"], b["close"], b["volume"], b["trades"]) == \
        (100.0, 100.0, 100.0, 100.0, 15.0, 2)


def test_fold_late_trade_moves_open_not_close():
    bars = {}
    fold(bars, [T(_et(10, 0, 30), 2, 101.0, 1)])
    fold(bars, [T(_et(10, 0, 10), 1, 99.0, 1)])
    b = bars[_et(10, 0)]
    assert (b["open"], b["close"], b["low"], b["high"]) == (99.0, 101.0, 99.0, 101.0)


def test_fold_into_plain_dict_bar_from_store():
    bars = {_et(10, 0): {"time": _et(10, 0), "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.5, "volume": 50.0}}
    fold(bars, [T(_et(10, 0, 40), 1, 102.0, 5)])
    b = bars[_et(10, 0)]
    assert (b["open"], b["high"], b["close"], b["volume"]) == (100.0, 102.0, 102.0, 55.0)
    assert isinstance(b, dict)
