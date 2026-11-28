import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from tastytrade.dxfeed import Candle

from app.providers import archive_minutes as archive


START = datetime(2024, 11, 29, 17, 59, tzinfo=timezone.utc)  # Early close at 18:00 UTC.
MS = int(START.timestamp() * 1000)


def candle(minute, *, symbol="SPY{=m}", begin=False, end=False, remove=False, pending=False, snip=False):
    return SimpleNamespace(event_symbol=symbol, time=MS + minute * 60_000,
                           snapshot_begin=begin, snapshot_end=end, snapshot_snip=snip,
                           remove=remove, pending=pending,
                           open=100, high=102, low=99, close=101, volume=14.25)


class FakeDX:
    def __init__(self, events, hang=False):
        self.events = events
        self.hang = hang
        self.calls = []

    async def subscribe_candle(self, symbols, interval, *, start_time, extended_trading_hours):
        self.calls.append(("add", symbols, interval, start_time, extended_trading_hours))

    async def unsubscribe_candle(self, symbol, interval, *, extended_trading_hours):
        self.calls.append(("remove", symbol, interval, extended_trading_hours))

    async def listen(self, cls):
        assert cls is Candle
        for event in self.events:
            yield event
        if self.hang:
            await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_closed_provider_minutes_across_early_close_require_a_fresh_snapshot():
    dx = FakeDX([candle(0, end=True), candle(2, symbol="QQQ{=m}", begin=True),
                 candle(3, begin=True), candle(2), candle(1), candle(0, end=True)])
    out = await archive.ArchiveMinuteSource(dx).fetch("SPY", START, START + timedelta(minutes=2, seconds=30))
    assert [b["time"] for b in out] == [MS, MS + 60_000]
    assert all(b["volume"] == 14.25 for b in out)
    assert dx.calls == [("add", ["SPY"], "1m", START, True), ("remove", "SPY", "1m", True)]


@pytest.mark.asyncio
async def test_replaced_snapshot_waits_for_transaction_end_and_applies_removals():
    dx = FakeDX([candle(3, begin=True), candle(2), candle(1, begin=True),
                 candle(0, end=True, pending=True, remove=True), candle(1, remove=True)])
    assert await archive.ArchiveMinuteSource(dx).fetch("SPY", START, START + timedelta(minutes=5)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [[candle(2, begin=True)], [candle(2, begin=True, snip=True)],
                                   [candle(2, begin=True, end=True)]])
async def test_unfinished_truncated_or_wrong_range_is_retried(events):
    dx = FakeDX(events)
    source = archive.ArchiveMinuteSource(dx)
    with pytest.raises(RuntimeError):
        await source.fetch("SPY", START, START + timedelta(minutes=3))
    assert dx.calls[-1] == ("remove", "SPY", "1m", True)
    with pytest.raises(RuntimeError, match="fresh archive connection"):
        await source.fetch("SPY", START, START + timedelta(minutes=3))


@pytest.mark.asyncio
async def test_silence_times_out_and_unsubscribes(monkeypatch):
    monkeypatch.setattr(archive, "_SNAPSHOT_SECONDS", 0.01)
    dx = FakeDX([candle(1, begin=True)], hang=True)
    with pytest.raises(TimeoutError):
        await archive.ArchiveMinuteSource(dx).fetch("SPY", START, START + timedelta(minutes=2))
    assert dx.calls[-1] == ("remove", "SPY", "1m", True)
