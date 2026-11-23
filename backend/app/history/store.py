"""Local SQLite store for deep price history (1-minute bars + derived daily bars).

Synchronous and thread-safe: every call opens its own short-lived connection
(callers use ``asyncio.to_thread``). WAL lets the API read while the sync job
writes. Bar shape everywhere: {"time": epoch ms UTC, open, high, low, close, volume}.
"""
import os
import sqlite3
import threading
from contextlib import closing

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m(
    symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY(symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars_1d(
    symbol TEXT, session TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,
    volume REAL, PRIMARY KEY(symbol, session)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_state(
    symbol TEXT PRIMARY KEY, status TEXT, note TEXT, last_full_check TEXT);
"""
_STATE_FIELDS = ("status", "note", "last_full_check")


def _row(r) -> dict:
    return {"time": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4], "volume": r[5]}


class HistoryStore:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with closing(self._connect()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def upsert_minutes(self, symbol: str, bars: list[dict]) -> int:
        rows = [(symbol, int(b["time"]), b["open"], b["high"], b["low"], b["close"],
                 b["volume"]) for b in bars]
        if not rows:
            return 0
        with closing(self._connect()) as c, c:
            c.executemany("INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?)", rows)
        return len(rows)

    def minutes(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        with closing(self._connect()) as c:
            cur = c.execute(
                "SELECT ts, open, high, low, close, volume FROM bars_1m "
                "WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                (symbol, start_ms, end_ms))
            return [_row(r) for r in cur]

    def upsert_daily(self, symbol: str, rows: list[dict]) -> None:
        data = [(symbol, r["session"], int(r["time"]), r["open"], r["high"], r["low"],
                 r["close"], r["volume"]) for r in rows]
        if not data:
            return
        with closing(self._connect()) as c, c:
            c.executemany("INSERT OR REPLACE INTO bars_1d VALUES (?,?,?,?,?,?,?,?)", data)

    def upsert_minutes_and_daily(self, symbol: str, bars: list[dict],
                                 daily_rows: list[dict]) -> int:
        """Upsert minutes and their rebuilt daily rows in ONE transaction: a crash or error
        between the two can never leave minutes stored without their daily rows."""
        minutes = [(symbol, int(b["time"]), b["open"], b["high"], b["low"], b["close"],
                    b["volume"]) for b in bars]
        days = [(symbol, r["session"], int(r["time"]), r["open"], r["high"], r["low"],
                 r["close"], r["volume"]) for r in daily_rows]
        with closing(self._connect()) as c, c:
            if minutes:
                c.executemany("INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?)", minutes)
            if days:
                c.executemany("INSERT OR REPLACE INTO bars_1d VALUES (?,?,?,?,?,?,?,?)", days)
        return len(minutes)

    def daily(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        with closing(self._connect()) as c:
            cur = c.execute(
                "SELECT ts, open, high, low, close, volume FROM bars_1d "
                "WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                (symbol, start_ms, end_ms))
            return [_row(r) for r in cur]

    def bounds(self, symbol: str) -> tuple[int | None, int | None]:
        with closing(self._connect()) as c:
            lo, hi = c.execute(
                "SELECT MIN(ts), MAX(ts) FROM bars_1m WHERE symbol=?", (symbol,)).fetchone()
        return lo, hi

    def delete_symbol(self, symbol: str) -> None:
        with closing(self._connect()) as c, c:
            for t in ("bars_1m", "bars_1d", "sync_state"):
                c.execute(f"DELETE FROM {t} WHERE symbol=?", (symbol,))

    def get_state(self, symbol: str) -> dict | None:
        with closing(self._connect()) as c:
            r = c.execute("SELECT status, note, last_full_check FROM sync_state "
                          "WHERE symbol=?", (symbol,)).fetchone()
        return dict(zip(_STATE_FIELDS, r)) if r else None

    def set_state(self, symbol: str, **fields) -> None:
        bad = set(fields) - set(_STATE_FIELDS)
        if bad:
            raise ValueError(f"unknown sync_state fields: {sorted(bad)}")
        with closing(self._connect()) as c, c:
            c.execute("INSERT OR IGNORE INTO sync_state(symbol) VALUES (?)", (symbol,))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                c.execute(f"UPDATE sync_state SET {sets} WHERE symbol=?",
                          (*fields.values(), symbol))


_store: HistoryStore | None = None
_lock = threading.Lock()


def get_store() -> HistoryStore:
    global _store
    path = get_settings().market_history_path
    with _lock:
        if _store is None or _store.path != path:
            _store = HistoryStore(path)
        return _store
