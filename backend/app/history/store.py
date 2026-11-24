"""Local SQLite store for deep price history (1-minute bars + derived daily bars).

Synchronous and thread-safe: every call opens its own short-lived connection
(callers use ``asyncio.to_thread``). WAL lets the API read while the sync job
writes. Bar shape everywhere: {"time": epoch ms UTC, open, high, low, close, volume}.
"""
import os
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta

from app.config import get_settings
from app.history.live_bars import ET, Trade, minute_of

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m(
    symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY(symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars_1d(
    symbol TEXT, session TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,
    volume REAL, PRIMARY KEY(symbol, session)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_state(
    symbol TEXT PRIMARY KEY, status TEXT, note TEXT, last_full_check TEXT);
CREATE TABLE IF NOT EXISTS ticks(
    symbol TEXT, ts_ms INTEGER, seq INTEGER, minute INTEGER, price REAL, size REAL,
    valid INTEGER, PRIMARY KEY(symbol, minute, seq)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS live_1m(
    symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
    trades INTEGER, PRIMARY KEY(symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sip_state(symbol TEXT PRIMARY KEY, sip_latest INTEGER);
"""
# Newest consolidated (SIP) minute per symbol; only ever moves forward.
_SET_SIP_LATEST = (
    "INSERT INTO sip_state(symbol, sip_latest) VALUES (?, ?) ON CONFLICT(symbol) "
    "DO UPDATE SET sip_latest = MAX(COALESCE(sip_latest, excluded.sip_latest), "
    "excluded.sip_latest)")
# Recompute live_1m rows from ticks. OHLC come from a minute's valid=1 trades when it has
# any, else from all its trades (first/last by ts_ms, seq); volume/trades cover ALL trades.
# {where} filters ticks; it must only reference symbol/minute.
_LIVE_RECOMPUTE = """
INSERT OR REPLACE INTO live_1m
WITH base AS (
    SELECT symbol, minute, ts_ms, seq, price, valid,
           SUM(size) OVER w AS vol, COUNT(*) OVER w AS n, MAX(valid) OVER w AS anyv
    FROM ticks WHERE {where}
    WINDOW w AS (PARTITION BY symbol, minute)),
pr AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY symbol, minute ORDER BY ts_ms, seq) AS rn_a,
           ROW_NUMBER() OVER (PARTITION BY symbol, minute ORDER BY ts_ms DESC, seq DESC) AS rn_d
    FROM base WHERE valid = 1 OR anyv = 0)
SELECT symbol, minute,
       MAX(CASE WHEN rn_a = 1 THEN price END), MAX(price), MIN(price),
       MAX(CASE WHEN rn_d = 1 THEN price END), MAX(vol), MAX(n)
FROM pr GROUP BY symbol, minute
"""


def _day_range(day: date) -> tuple[int, int]:
    """[ET midnight of day, ET midnight of the next day) in epoch ms (DST-safe)."""
    lo = datetime.combine(day, dtime(0), tzinfo=ET)
    hi = datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=ET)
    return int(lo.timestamp()) * 1000, int(hi.timestamp()) * 1000

_STATE_FIELDS = ("status", "note", "last_full_check")
_TICKS_PK = ("symbol", "minute", "seq")


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
            pk = [r[1] for r in sorted((r for r in c.execute("PRAGMA table_info(ticks)")
                                        if r[5]), key=lambda r: r[5])]
            if pk and pk != list(_TICKS_PK):
                # An older key: ticks only ever hold same-day trades, so start it fresh
                # (live_1m is left as is). Dropping the table drops its old index too.
                c.execute("DROP TABLE ticks")
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
                                 daily_rows: list[dict], sip_latest: int | None = None) -> int:
        """Upsert minutes and their rebuilt daily rows in ONE transaction: a crash or error
        between the two can never leave minutes stored without their daily rows.
        `sip_latest` (the newest SIP minute in `bars`) is recorded in the same transaction."""
        minutes = [(symbol, int(b["time"]), b["open"], b["high"], b["low"], b["close"],
                    b["volume"]) for b in bars]
        days = [(symbol, r["session"], int(r["time"]), r["open"], r["high"], r["low"],
                 r["close"], r["volume"]) for r in daily_rows]
        with closing(self._connect()) as c, c:
            if minutes:
                c.executemany("INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?)", minutes)
            if days:
                c.executemany("INSERT OR REPLACE INTO bars_1d VALUES (?,?,?,?,?,?,?,?)", days)
            if sip_latest is not None:
                c.execute(_SET_SIP_LATEST, (symbol, int(sip_latest)))
        return len(minutes)

    def set_sip_latest(self, symbol: str, ts: int) -> None:
        with closing(self._connect()) as c, c:
            c.execute(_SET_SIP_LATEST, (symbol, int(ts)))

    def sip_latest(self, symbol: str) -> int | None:
        """Time of the newest consolidated (SIP) minute stored for `symbol`, if known."""
        with closing(self._connect()) as c:
            r = c.execute("SELECT sip_latest FROM sip_state WHERE symbol=?",
                          (symbol,)).fetchone()
        return r[0] if r else None

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
            for t in ("bars_1m", "bars_1d", "sync_state", "ticks", "live_1m", "sip_state"):
                c.execute(f"DELETE FROM {t} WHERE symbol=?", (symbol,))

    def insert_trades(self, trades: list[Trade]) -> set[tuple[str, int]]:
        """Store trades (duplicates ignored) and recompute every touched live minute from
        ``ticks``, all in one transaction. Returns the touched (symbol, minute) pairs."""
        rows = [(t.symbol, t.ts_ms, t.seq, minute_of(t.ts_ms), t.price, t.size,
                 1 if t.valid else 0) for t in trades]
        touched = {(r[0], r[3]) for r in rows}
        if not rows:
            return touched
        with closing(self._connect()) as c, c:
            c.executemany("INSERT OR IGNORE INTO ticks VALUES (?,?,?,?,?,?,?)", rows)
            for symbol, minute in touched:
                c.execute(_LIVE_RECOMPUTE.format(where="symbol = ? AND minute = ?"),
                          (symbol, minute))
        return touched

    def live_minutes(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        with closing(self._connect()) as c:
            cur = c.execute(
                "SELECT ts, open, high, low, close, volume FROM live_1m "
                "WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                (symbol, start_ms, end_ms))
            return [_row(r) for r in cur]

    def rebuild_live(self, day: date) -> int:
        lo, hi = _day_range(day)
        with closing(self._connect()) as c, c:
            c.execute("DELETE FROM live_1m WHERE ts>=? AND ts<?", (lo, hi))
            c.execute(_LIVE_RECOMPUTE.format(where="minute >= ? AND minute < ?"), (lo, hi))
            return c.execute("SELECT COUNT(*) FROM live_1m WHERE ts>=? AND ts<?",
                             (lo, hi)).fetchone()[0]

    def live_days(self) -> list[date]:
        with closing(self._connect()) as c:
            mins = c.execute("SELECT DISTINCT ts FROM live_1m UNION "
                             "SELECT DISTINCT minute FROM ticks").fetchall()
        return sorted({datetime.fromtimestamp(m[0] / 1000, ET).date() for m in mins})

    def delete_live_day(self, day: date) -> None:
        lo, hi = _day_range(day)
        with closing(self._connect()) as c, c:
            c.execute("DELETE FROM ticks WHERE minute>=? AND minute<?", (lo, hi))
            c.execute("DELETE FROM live_1m WHERE ts>=? AND ts<?", (lo, hi))

    def delete_ticks_day(self, day: date) -> None:
        """Drop one ET day's raw trades but keep its live_1m rows (a finalize retry marker)."""
        lo, hi = _day_range(day)
        with closing(self._connect()) as c, c:
            c.execute("DELETE FROM ticks WHERE minute>=? AND minute<?", (lo, hi))

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
