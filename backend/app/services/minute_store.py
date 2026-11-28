"""Durable one-minute OHLCV and archive progress in the application's database."""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.base import engine, session_scope
from app.db.models import MinuteArchiveSymbol, MinuteBar

_SYMBOLS = MinuteArchiveSymbol.__table__
_BARS = MinuteBar.__table__
_OHLCV = ("open", "high", "low", "close", "volume")
_ET = ZoneInfo("America/New_York")


async def init_archive() -> None:
    """Create only the archive tables for a standalone backfill."""
    async with engine.begin() as conn:
        await conn.run_sync(_BARS.metadata.create_all, tables=[_SYMBOLS, _BARS])


def _insert(session, table):
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgres_insert
    return insert(table)


async def archive_start(symbol: str, first_day: date) -> date:
    """Remember where to start even if the first download fails and the app restarts."""
    read = select(_SYMBOLS.c.first_day).where(_SYMBOLS.c.symbol == symbol)
    async with session_scope() as session:
        existing = await session.scalar(read)
    if existing is not None:
        return existing
    async with session_scope() as session:
        insert = _insert(session, _SYMBOLS).values(symbol=symbol, first_day=first_day)
        await session.execute(insert.on_conflict_do_update(
            index_elements=["symbol"], set_={"first_day": insert.excluded.first_day},
            where=_SYMBOLS.c.first_day.is_(None)))
        return (await session.scalars(read)).one()


async def saved_through(symbol: str) -> date | None:
    async with session_scope() as session:
        return await session.scalar(select(_SYMBOLS.c.completed_day).where(_SYMBOLS.c.symbol == symbol))


async def latest_minute(symbol: str) -> int | None:
    async with session_scope() as session:
        return await session.scalar(select(func.max(_BARS.c.time)).where(_BARS.c.symbol == symbol))


async def save_minutes(symbol: str, bars: Iterable[dict], *, completed_day: date | None = None) -> None:
    """Upsert a partial batch or atomically finalize a confirmed extended session.

    The caller supplies normalized symbols and closed, minute-aligned candles.
    A completed batch must cover the whole 04:00–20:00 New York session so
    that superseded provisional candles can be removed, including on empty days.
    """
    by_time = {}
    for bar in bars:
        ts = int(bar["time"])
        if ts <= 0 or ts != bar["time"] or ts % 60_000:
            raise ValueError("Invalid archive minute timestamp")
        values = {field: float(bar[field]) for field in _OHLCV}
        if (not all(map(math.isfinite, values.values())) or values["volume"] < 0
                or values["low"] <= 0 or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            raise ValueError("Invalid archive OHLCV")
        by_time[ts] = {"symbol": symbol, "time": ts, **values}

    async with session_scope() as session:
        progress = _insert(session, _SYMBOLS).values(symbol=symbol)
        await session.execute(progress.on_conflict_do_nothing(index_elements=["symbol"]))
        # SQLite holds the writer lock from the insert. On PostgreSQL, lock
        # this symbol so simultaneous retries, partials and finalizations serialize.
        stmt = select(_SYMBOLS.c.completed_day).where(_SYMBOLS.c.symbol == symbol)
        checkpoint = await session.scalar(stmt.with_for_update())
        if completed_day is not None:
            if checkpoint is not None and completed_day <= checkpoint:
                return
            start = int(datetime.combine(completed_day, time(4), _ET).timestamp() * 1000)
            end = int(datetime.combine(completed_day, time(20), _ET).timestamp() * 1000)
            if any(ts < start or ts >= end for ts in by_time):
                raise ValueError("Final archive batch contains a minute outside its session")
            await session.execute(delete(_BARS).where(
                _BARS.c.symbol == symbol, _BARS.c.time >= start, _BARS.c.time < end))
            await session.execute(update(_SYMBOLS).where(_SYMBOLS.c.symbol == symbol)
                                  .values(completed_day=completed_day))
        elif checkpoint is not None:
            next_day = datetime.combine(checkpoint + timedelta(days=1), time.min, _ET)
            first_unfinished_ms = int(next_day.timestamp() * 1000)
            by_time = {ts: row for ts, row in by_time.items() if ts >= first_unfinished_ms}
        if by_time:
            write = _insert(session, _BARS)
            write = write.on_conflict_do_update(index_elements=["symbol", "time"],
                                                set_={name: write.excluded[name] for name in _OHLCV})
            await session.execute(write, [by_time[t] for t in sorted(by_time)])


async def read_minutes(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Read [start_ms, end_ms) in ascending timestamp order."""
    columns = [_BARS.c[name] for name in ("time", *_OHLCV)]
    stmt = select(*columns).where(_BARS.c.symbol == symbol, _BARS.c.time >= start_ms,
                                  _BARS.c.time < end_ms).order_by(_BARS.c.time)
    async with session_scope() as session:
        result = await session.execute(stmt)
        return [dict(row) for row in result.mappings()]
