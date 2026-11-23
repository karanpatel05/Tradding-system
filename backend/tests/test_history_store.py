from app.config import get_settings
from app.history.store import HistoryStore


def _bar(t, close=1.0):
    return {"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": close, "volume": 10.0}


def test_upsert_is_idempotent_and_ordered(tmp_path):
    s = HistoryStore(str(tmp_path / "h.db"))
    assert s.upsert_minutes("SPY", [_bar(180_000), _bar(60_000), _bar(120_000)]) == 3
    s.upsert_minutes("SPY", [_bar(120_000, close=9.0)])
    rows = s.minutes("SPY", 0, 10**9)
    assert [r["time"] for r in rows] == [60_000, 120_000, 180_000]
    assert rows[1]["close"] == 9.0
    assert set(rows[0]) == {"time", "open", "high", "low", "close", "volume"}


def test_minutes_range_is_half_open(tmp_path):
    s = HistoryStore(str(tmp_path / "h.db"))
    s.upsert_minutes("SPY", [_bar(60_000), _bar(120_000), _bar(180_000)])
    assert [r["time"] for r in s.minutes("SPY", 60_000, 180_000)] == [60_000, 120_000]


def test_bounds_and_delete(tmp_path):
    s = HistoryStore(str(tmp_path / "h.db"))
    assert s.bounds("SPY") == (None, None)
    s.upsert_minutes("SPY", [_bar(120_000), _bar(60_000)])
    s.upsert_daily("SPY", [{**_bar(5), "session": "2024-01-02"}])
    s.set_state("SPY", status="ok")
    assert s.bounds("SPY") == (60_000, 120_000)
    s.delete_symbol("SPY")
    assert s.bounds("SPY") == (None, None)
    assert s.get_state("SPY") is None
    assert s.daily("SPY", 0, 10**9) == []


def test_daily_round_trip_and_replace(tmp_path):
    s = HistoryStore(str(tmp_path / "h.db"))
    s.upsert_daily("SPY", [{**_bar(200), "session": "2024-01-03"},
                           {**_bar(100), "session": "2024-01-02"}])
    s.upsert_daily("SPY", [{**_bar(100, close=7.0), "session": "2024-01-02"}])
    rows = s.daily("SPY", 0, 10**9)
    assert [r["time"] for r in rows] == [100, 200]
    assert rows[0]["close"] == 7.0
    assert [r["time"] for r in s.daily("SPY", 100, 200)] == [100]


def test_state_round_trip(tmp_path):
    s = HistoryStore(str(tmp_path / "h.db"))
    assert s.get_state("SPY") is None
    s.set_state("SPY", status="ok", note="")
    s.set_state("SPY", note="x")
    st = s.get_state("SPY")
    assert st["status"] == "ok" and st["note"] == "x" and "last_full_check" in st


def test_path_attribute(tmp_path):
    p = str(tmp_path / "h.db")
    assert HistoryStore(p).path == p


def test_tests_never_use_real_history_file():
    assert not get_settings().market_history_path.replace("\\", "/").endswith(
        "backend/market_history.db")


def test_store_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "h.db"
    s = HistoryStore(str(path))
    assert path.exists() and s.bounds("SPY") == (None, None)


def test_minutes_and_daily_are_written_together(tmp_path):
    s = HistoryStore(str(tmp_path / "h.db"))
    n = s.upsert_minutes_and_daily("SPY", [_bar(60_000), _bar(120_000)],
                                   [{**_bar(60_000), "session": "1970-01-01"}])
    assert n == 2
    assert [r["time"] for r in s.minutes("SPY", 0, 10**9)] == [60_000, 120_000]
    assert [r["time"] for r in s.daily("SPY", 0, 10**9)] == [60_000]


def test_failed_daily_write_commits_no_minutes(tmp_path):
    """F9: one transaction. A failure on the daily insert leaves neither table changed."""
    import sqlite3

    import pytest

    s = HistoryStore(str(tmp_path / "h.db"))
    with sqlite3.connect(s.path) as c:
        c.execute("DROP TABLE bars_1d")          # the daily insert will now fail
    with pytest.raises(sqlite3.OperationalError):
        s.upsert_minutes_and_daily("SPY", [_bar(60_000)], [{**_bar(60_000), "session": "1970-01-01"}])
    assert s.minutes("SPY", 0, 10**9) == []
