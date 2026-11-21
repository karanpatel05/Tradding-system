"""The overview's weekly % must never block on the candle feed.

Candle collection is serialized and slow (seconds per symbol), so the weekly refresh
runs as ONE background task: callers get cached (even stale) values immediately, and
concurrent polls never start a second refresh."""
import asyncio

import pytest

from app.services import market


class SlowProvider:
    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.calls: list[str] = []

    async def get_bars(self, sym, interval, start=None):
        self.calls.append(sym)
        await asyncio.sleep(self.delay)
        return [{"close": 100.0 + i} for i in range(6)]  # 105/100 - 1 = 0.05


@pytest.fixture
def slow_provider(monkeypatch):
    market._weekly_cache.clear()
    market._weekly_task = None
    provider = SlowProvider()
    monkeypatch.setattr(market, "_history_provider", lambda: provider)

    async def quotes(symbols, timeout=2.5):
        return {s: {"last": 1.1, "close": 1.0, "price": 1.1} for s in symbols}

    monkeypatch.setattr(market, "facade_stock_quotes", quotes)
    yield provider
    market._weekly_cache.clear()
    market._weekly_task = None


@pytest.mark.asyncio
async def test_concurrent_overviews_do_not_wait_and_share_one_refresh(slow_provider):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    outs = await asyncio.gather(*(market.get_market_overview() for _ in range(3)))
    assert loop.time() - t0 < 0.2  # nobody waited on the 0.3s-per-symbol feed
    for out in outs:
        assert out["data_available"] is True
        assert all(ix["week_change_pct"] is None for ix in out["indices"])

    await market._weekly_task
    symbols = [ix["symbol"] for ix in market.INDICES]
    assert sorted(slow_provider.calls) == sorted(symbols)  # exactly one set of fetches

    out = await market.get_market_overview()
    assert all(ix["week_change_pct"] == 0.05 for ix in out["indices"])


@pytest.mark.asyncio
async def test_stale_values_are_served_while_refreshing(slow_provider, monkeypatch):
    for sym in (ix["symbol"] for ix in market.INDICES):
        market._weekly_cache[sym] = (0.01, market.monotonic() - market._WEEKLY_TTL - 1)

    out = await market.get_market_overview()
    assert all(ix["week_change_pct"] == 0.01 for ix in out["indices"])  # stale, not None
    assert market._weekly_task is not None and not market._weekly_task.done()

    await market._weekly_task
    assert market.cached_week_change("SPY") == 0.05


@pytest.mark.asyncio
async def test_stream_reader_triggers_refresh_when_stale(slow_provider):
    assert market.cached_week_change("SPY") is None
    assert market._weekly_task is not None  # the WS snapshot path kicks the refresh too
    await market._weekly_task
    assert market.cached_week_change("SPY") == 0.05
    assert len(slow_provider.calls) == len(market.INDICES)


@pytest.mark.asyncio
async def test_refresh_failure_is_contained(monkeypatch, slow_provider):
    async def boom(sym, interval, start=None):
        raise RuntimeError("feed down")

    monkeypatch.setattr(slow_provider, "get_bars", boom)
    out = await market.get_market_overview()
    assert out["data_available"] is True
    await market._weekly_task  # must not raise
    assert market.cached_week_change("SPY") is None
