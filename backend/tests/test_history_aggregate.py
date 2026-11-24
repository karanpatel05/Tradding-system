from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.history.aggregate import (
    bucket,
    daily_from_minutes,
    et_session_start_ms,
    weekly_from_daily,
)

ET = ZoneInfo("America/New_York")


def _ms(et_str: str) -> int:
    dt = datetime.strptime(et_str, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return int(dt.timestamp() * 1000)


def m(et_str: str, close: float) -> dict:
    return {"time": _ms(et_str), "open": close - 0.5, "high": close + 1,
            "low": close - 1, "close": close, "volume": 10}


def _et(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ET).strftime("%Y-%m-%d %H:%M")


def test_5m_buckets_on_clock():
    bars = [m(f"2026-11-20 09:{30 + i}", 100 + i) for i in range(10)]
    out = bucket(bars, "5m")
    assert [_et(b["time"]) for b in out] == ["2026-11-20 09:30", "2026-11-20 09:35"]
    first = out[0]
    assert first["open"] == 99.5 and first["close"] == 104
    assert first["high"] == 105 and first["low"] == 99
    assert first["volume"] == 50
    assert out[1]["close"] == 109


def test_1m_returns_input():
    bars = [m("2026-11-20 09:30", 100)]
    assert bucket(bars, "1m") == bars


def test_1h_and_4h_on_et_clock():
    bars = [m(f"2026-11-20 07:{n}", 100) for n in (58, 59)]
    bars += [m(f"2026-11-20 08:0{n}", 101) for n in (0, 1, 2)]
    bars += [m("2026-11-20 09:30", 102), m("2026-11-20 10:01", 103)]
    assert [_et(b["time"]) for b in bucket(bars, "1h")] == [
        "2026-11-20 07:00", "2026-11-20 08:00", "2026-11-20 09:00", "2026-11-20 10:00"]
    assert [_et(b["time"]) for b in bucket(bars, "4h")] == [
        "2026-11-20 04:00", "2026-11-20 08:00"]


def test_buckets_follow_et_across_dst():
    bars = [m("2026-03-06 10:00", 100), m("2026-03-09 10:00", 100)]
    assert bars[0]["time"] % 86400000 != bars[1]["time"] % 86400000
    out = bucket(bars, "1h")
    assert [_et(b["time"]) for b in out] == ["2026-03-06 10:00", "2026-03-09 10:00"]


def test_daily_excludes_extended_hours():
    bars = [m("2026-11-20 08:00", 90), m("2026-11-20 09:30", 100),
            m("2026-11-20 15:59", 110), m("2026-11-20 16:00", 120)]
    out = daily_from_minutes(bars)
    assert len(out) == 1
    d = out[0]
    assert d["session"] == "2026-11-20"
    assert d["time"] == et_session_start_ms(date(2026, 11, 20))
    assert d["open"] == 99.5 and d["close"] == 110
    assert d["high"] == 111 and d["low"] == 99
    assert d["volume"] == 20


def test_half_day_daily_bar():
    bars = [m(f"2026-11-27 {9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}", 100 + i)
            for i in range(210)]
    assert _et(bars[-1]["time"]) == "2026-11-27 12:59"
    out = daily_from_minutes(bars)
    assert len(out) == 1 and out[0]["close"] == bars[-1]["close"]


def test_weekly_starts_monday():
    days = daily_from_minutes([
        m("2026-11-20 10:00", 100), m("2026-11-23 10:00", 110), m("2026-11-24 10:00", 120)])
    out = weekly_from_daily(days)
    assert len(out) == 2
    assert out[1]["time"] == days[1]["time"]
    assert out[1]["open"] == days[1]["open"] and out[1]["close"] == 120
    assert out[1]["high"] == 121 and out[1]["volume"] == 20
