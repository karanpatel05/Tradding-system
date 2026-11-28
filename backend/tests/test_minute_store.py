import asyncio
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import minute_store as archive

DAY = date(2026, 11, 24)
PRE = int(datetime.fromisoformat("2026-11-24T04:00:00-05:00").timestamp() * 1000)
MINUTE = 60_000


@pytest_asyncio.fixture
async def database(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'minutes.db'}")
    monkeypatch.setattr(archive, "engine", engine)
    monkeypatch.setattr(archive, "session_scope", async_sessionmaker(engine).begin)
    await archive.init_archive()
    try:
        yield engine
    finally:
        await engine.dispose()


def bar(time, close=101.0):
    return {"time": time, "open": 100.0, "high": 103.0, "low": 99.0,
            "close": close, "volume": 25.75}


@pytest.mark.asyncio
async def test_initial_cursor_survives_retries_and_does_not_mark_a_day_complete(database):
    async with database.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert set(tables) == {"market_bars_1m", "minute_archive_symbols"}
    assert await archive.latest_minute("SPY") is None
    assert await archive.saved_through("SPY") is None
    assert await archive.read_minutes("SPY", 0, PRE) == []
    assert await archive.archive_start("SPY", DAY) == DAY
    assert await archive.archive_start("SPY", date(2026, 11, 25)) == DAY
    assert await archive.saved_through("SPY") is None


@pytest.mark.asyncio
async def test_partial_and_final_retries_preserve_authoritative_minutes(database):
    t1, t2 = PRE + MINUTE, PRE + 2 * MINUTE
    await archive.save_minutes("SPY", [bar(t2), bar(PRE), bar(PRE, 100.5)])
    assert await archive.read_minutes("SPY", PRE, t2) == [bar(PRE, 100.5)]
    assert await archive.latest_minute("SPY") == t2
    await archive.save_minutes("QQQ", [bar(t2)])
    await archive.save_minutes("SPY", [bar(PRE - MINUTE)])
    final = [bar(t1), bar(PRE, 102.0)]
    await asyncio.gather(archive.save_minutes("SPY", final, completed_day=DAY),
                         archive.save_minutes("SPY", [bar(t2)]))
    await archive.save_minutes("SPY", [bar(PRE, 99.0), bar(t2)])
    await archive.save_minutes("SPY", [], completed_day=DAY)
    assert await archive.saved_through("SPY") == DAY
    assert await archive.latest_minute("SPY") == t1
    assert await archive.read_minutes("SPY", PRE - MINUTE, t2 + 1) == [
        bar(PRE - MINUTE), bar(PRE, 102.0), bar(t1)]
    assert await archive.read_minutes("QQQ", PRE, t2 + 1) == [bar(t2)]
    await archive.save_minutes("QQQ", [], completed_day=DAY)
    assert await archive.saved_through("QQQ") == DAY
    assert await archive.latest_minute("QQQ") is None


@pytest.mark.asyncio
async def test_finalization_rolls_back_bars_and_checkpoint_together(database):
    await archive.save_minutes("SPY", [bar(PRE)])
    # Fail the second row in the bulk write after the provisional rows were removed.
    async with database.begin() as conn:
        await conn.execute(text(f"""
            CREATE TRIGGER interrupt_archive BEFORE INSERT ON market_bars_1m
            WHEN new.time = {PRE + MINUTE}
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END
        """))
    with pytest.raises(IntegrityError):
        await archive.save_minutes("SPY", [bar(PRE, 102), bar(PRE + MINUTE)], completed_day=DAY)
    assert await archive.saved_through("SPY") is None
    assert await archive.read_minutes("SPY", PRE, PRE + MINUTE * 2) == [bar(PRE)]
    with pytest.raises(ValueError):
        await archive.save_minutes("SPY", [bar(PRE - MINUTE)], completed_day=DAY)
    assert await archive.saved_through("SPY") is None
