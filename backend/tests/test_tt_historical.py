"""TastytradeHistoricalProvider normalizes streamer candles into provider Bars, and the
factory returns it unconditionally (Tastytrade is the sole historical data source)."""
from datetime import datetime, timezone

import pytest

import app.providers as providers
from app.providers.tastytrade_historical import TastytradeHistoricalProvider


@pytest.mark.asyncio
async def test_get_bars_delegates_to_streamer(monkeypatch):
    p = TastytradeHistoricalProvider()
    async def fake_candles(symbol, interval, start, end=None, idle_seconds=2.0):
        return [{"time": 1000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                 "volume": 10.0, "symbol": symbol}]
    monkeypatch.setattr(p, "_candles", fake_candles)
    bars = await p.get_bars("QQQ", "5m", start=datetime(2026, 7, 8, tzinfo=timezone.utc))
    assert bars[0]["close"] == 1.5 and bars[0]["symbol"] == "QQQ"


def test_factory_returns_tt_unconditionally():
    providers.get_historical_provider.cache_clear()
    assert providers.get_historical_provider().name == "tastytrade"
    providers.get_historical_provider.cache_clear()
