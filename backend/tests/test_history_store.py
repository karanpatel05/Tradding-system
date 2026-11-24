import sqlite3
from contextlib import closing
from datetime import date, datetime

import pytest

from app.config import get_settings
from app.history.live_bars import ET, Trade
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


D1 = date(2025, 1, 6)
D2 = date(2025, 1, 7)
D_TODAY = date(2025, 1, 8)


@pytest.fixture
def store(tmp_path):
    return HistoryStore(str(tmp_path / "h.db"))


def T(ts, seq, price, size, symbol="SPY", valid=True):
    return Trade(symbol, ts, seq, price, size, valid)


def _et(h, m, s=0, ms=0, day=D_TODAY):
    dt = datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET)
    return int(dt.timestamp()) * 1000 + ms


def test_insert_ticks_ignores_duplicates_and_live_matches_ticks(store):
    ts = [T(_et(10, 0, 1), 1, 100.0, 10), T(_et(10, 0, 2), 2, 101.0, 5)]
    store.insert_trades(ts)
    store.insert_trades(ts)  # redelivery
    assert store.live_minutes("SPY", 0, 2**62) == [
        {"time": _et(10, 0), "open": 100.0, "high": 101.0, "low": 100.0,
         "close": 101.0, "volume": 15.0}]


def test_insert_trades_returns_touched_minutes_and_handles_late_trade(store):
    touched = store.insert_trades([T(_et(10, 0, 5), 5, 100.0, 1), T(_et(10, 1, 0), 6, 102.0, 1)])
    assert touched == {("SPY", _et(10, 0)), ("SPY", _et(10, 1))}
    store.insert_trades([T(_et(10, 0, 1), 1, 99.0, 1)])  # late, earlier in minute
    bar = store.live_minutes("SPY", _et(10, 0), _et(10, 1))[0]
    assert bar["open"] == 99.0 and bar["close"] == 100.0 and bar["volume"] == 2.0


def test_closing_print_groups_into_1559(store):
    store.insert_trades([T(_et(16, 0), 1, 100.0, 1)])
    assert [b["time"] for b in store.live_minutes("SPY", 0, 2**62)] == [_et(15, 59)]


def test_live_minute_prices_ignore_odd_lots(store):
    store.insert_trades([T(_et(10, 0, 1), 1, 100.0, 100),
                         T(_et(10, 0, 2), 2, 105.0, 5, valid=False),
                         T(_et(10, 0, 3), 3, 101.0, 100)])
    bar = store.live_minutes("SPY", 0, 2**62)[0]
    assert bar["high"] == 101.0 and bar["close"] == 101.0 and bar["volume"] == 205.0


def test_live_minute_all_invalid_falls_back_to_all_trades(store):
    store.insert_trades([T(_et(10, 0, 1), 1, 100.0, 1, valid=False),
                         T(_et(10, 0, 2), 2, 102.0, 1, valid=False)])
    bar = store.live_minutes("SPY", 0, 2**62)[0]
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100.0, 102.0, 100.0, 102.0)


def test_live_tables_do_not_touch_bars_1m(store):
    store.insert_trades([T(_et(10, 0, 1), 1, 100.0, 10)])
    assert store.minutes("SPY", 0, 2**62) == []


def test_live_days_and_delete(store):
    store.insert_trades([T(_et(10, 0, 1, day=D1), 1, 1.0, 1),
                         T(_et(10, 0, 1, day=D2), 1 + 1, 1.0, 1)])
    assert store.live_days() == [D1, D2]
    store.delete_live_day(D1)
    assert store.live_days() == [D2]
    with closing(store._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1


def test_rebuild_live_from_ticks(store):
    store.insert_trades([T(_et(10, 0, 1), 1, 100.0, 10)])
    with closing(store._connect()) as c, c:
        c.execute("DELETE FROM live_1m")
    assert store.rebuild_live(D_TODAY) == 1 and len(store.live_minutes("SPY", 0, 2**62)) == 1


def test_delete_symbol_clears_live_tables(store):
    store.insert_trades([T(_et(10, 0, 1), 1, 100.0, 10)])
    store.delete_symbol("SPY")
    assert store.live_minutes("SPY", 0, 2**62) == [] and store.live_days() == []


def _ticks_pk(store):
    with closing(store._connect()) as c:
        cols = c.execute("PRAGMA table_info(ticks)").fetchall()
    return [r[1] for r in sorted((r for r in cols if r[5]), key=lambda r: r[5])]


def test_ticks_primary_key_is_symbol_minute_seq_without_extra_index(store):
    assert _ticks_pk(store) == ["symbol", "minute", "seq"]
    with closing(store._connect()) as c:
        idx = c.execute("SELECT name FROM sqlite_master WHERE type='index' "
                        "AND tbl_name='ticks' AND sql IS NOT NULL").fetchall()
    assert idx == []


def test_old_ticks_table_is_recreated_and_live_1m_kept(tmp_path):
    path = str(tmp_path / "old.db")
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript("""
            CREATE TABLE ticks(symbol TEXT, ts_ms INTEGER, seq INTEGER, minute INTEGER,
                price REAL, size REAL, valid INTEGER, PRIMARY KEY(symbol, seq)) WITHOUT ROWID;
            CREATE INDEX ticks_symbol_minute ON ticks(symbol, minute);
            CREATE TABLE live_1m(symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL,
                close REAL, volume REAL, trades INTEGER, PRIMARY KEY(symbol, ts)) WITHOUT ROWID;
            INSERT INTO ticks VALUES ('SPY', 1, 1, 0, 1.0, 1.0, 1);
            INSERT INTO live_1m VALUES ('SPY', 0, 1, 1, 1, 1, 1, 1);
        """)
    s = HistoryStore(path)
    assert _ticks_pk(s) == ["symbol", "minute", "seq"]
    with closing(s._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name="
                         "'ticks_symbol_minute'").fetchone()[0] == 0
    assert len(s.live_minutes("SPY", 0, 1)) == 1
    s2 = HistoryStore(path)          # reopening a new-key table keeps its rows
    s2.insert_trades([T(_et(10, 0, 1), 1, 100.0, 10)])
    assert len(HistoryStore(path).live_minutes("SPY", 0, 2**62)) == 2
    with closing(s2._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1
