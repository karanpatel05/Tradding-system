from datetime import datetime, timezone

from app.providers.ibkr_historical import normalize_bar


class _B:  # ib_async BarData with epoch-seconds date (formatDate=2)
    def __init__(self, t, o, h, l, c, v):
        self.date, self.open, self.high, self.low, self.close, self.volume = t, o, h, l, c, v


def test_normalize_bar_epoch_seconds():
    ts = int(datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc).timestamp())
    b = normalize_bar(_B(ts, 10, 11, 9, 10.5, 1000), "NVDA")
    assert b == {"time": ts * 1000, "open": 10.0, "high": 11.0, "low": 9.0,
                 "close": 10.5, "volume": 1000.0, "symbol": "NVDA"}


def test_normalize_bar_daily_date():
    from datetime import date
    b = normalize_bar(_B(date(2025, 1, 2), 10, 11, 9, 10.5, 1000), "AMD")
    assert b["symbol"] == "AMD" and b["open"] == 10.0
    # daily date -> midnight ms
    assert b["time"] == int(datetime(2025, 1, 2).timestamp() * 1000)
