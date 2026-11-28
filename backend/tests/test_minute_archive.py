from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import minute_archive as archive


FRIDAY = date(2026, 11, 27)  # Thanksgiving Friday, 13:00 ET close


def at(day, hour, minute=0, second=0):
    return datetime.combine(day, time(hour, minute, second), archive.ET)


@pytest.mark.parametrize("when,expected", [
    (at(FRIDAY, 12, 59), date(2026, 11, 25)),
    (at(FRIDAY, 13), FRIDAY),
    (at(date(2026, 11, 29), 10), FRIDAY),
    (at(date(2026, 11, 30), 15, 59), FRIDAY),
])
def test_last_regular_close_observes_holidays_and_half_days(when, expected):
    assert archive._latest_closed_session(when) == expected


@pytest.mark.asyncio
async def test_partial_saves_closed_minutes_then_replaces_with_a_full_session(monkeypatch):
    source = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    save = AsyncMock()
    last = AsyncMock(return_value=None)
    monkeypatch.setattr(archive.minute_store, "save_minutes", save)
    monkeypatch.setattr(archive.minute_store, "latest_minute", last)

    await archive._save_session(source, "SPY", FRIDAY, at(FRIDAY, 13, 4, 36))
    source.fetch.assert_awaited_with("SPY", at(FRIDAY, 4), at(FRIDAY, 13, 4))
    save.assert_awaited_with("SPY", [], completed_day=None)

    last.return_value = int(at(FRIDAY, 13, 3).timestamp() * 1000)
    await archive._save_session(source, "SPY", FRIDAY, at(FRIDAY, 13, 9, 10))
    source.fetch.assert_awaited_with("SPY", at(FRIDAY, 13, 1), at(FRIDAY, 13, 9))

    await archive._save_session(source, "SPY", FRIDAY, at(FRIDAY, 20, 20))
    source.fetch.assert_awaited_with("SPY", at(FRIDAY, 4), at(FRIDAY, 20))
    save.assert_awaited_with("SPY", [], completed_day=FRIDAY)


@pytest.mark.asyncio
async def test_failed_symbol_retries_from_its_checkpoint_without_skipping_a_session(monkeypatch):
    first = date(2026, 11, 25)
    through = {}
    fail_spy = True
    fetched = []

    class Source:
        async def fetch(self, symbol, start, end):
            fetched.append((symbol, start.date()))
            if symbol == "SPY" and start.date() == first and fail_spy:
                raise TimeoutError
            return []

    @asynccontextmanager
    async def open_source():
        yield Source()

    async def save(symbol, bars, *, completed_day):
        if completed_day:
            through[symbol] = completed_day

    monkeypatch.setattr(archive, "get_settings", lambda: SimpleNamespace(market_archive_symbols="SPY,qqq,SPY"))
    monkeypatch.setattr(archive, "open_minute_source", open_source)
    monkeypatch.setattr(archive.minute_store, "archive_start", AsyncMock(return_value=first))
    monkeypatch.setattr(archive.minute_store, "latest_minute", AsyncMock(return_value=None))
    monkeypatch.setattr(archive.minute_store, "saved_through", AsyncMock(side_effect=through.get))
    monkeypatch.setattr(archive.minute_store, "save_minutes", save)

    now = at(FRIDAY + timedelta(days=1), 12)
    assert not await archive.archive_once(now)
    assert through == {"QQQ": FRIDAY}
    assert fetched == [("SPY", FRIDAY), ("SPY", first), ("QQQ", FRIDAY), ("QQQ", first), ("QQQ", FRIDAY)]
    fail_spy = False
    fetched.clear()
    assert await archive.archive_once(now)
    assert fetched == [("SPY", FRIDAY), ("SPY", first), ("SPY", FRIDAY)]
    fetched.clear()
    assert await archive.archive_once(now)
    assert not fetched
