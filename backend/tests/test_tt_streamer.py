"""TTStreamer against a fake DXLink streamer: subscribe-on-demand, cached quote reads,
trade last-price, candle collection into normalized Bars. No network."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.tastytrade import streamer as st_mod


class _FakeDx:
    """Stands in for DXLinkStreamer: events are pushed by the test into per-type queues."""
    def __init__(self):
        self.queues = {}
        self.subscribed = []

    def _q(self, cls):
        return self.queues.setdefault(cls.__name__, asyncio.Queue())

    async def subscribe(self, cls, symbols):
        self.subscribed.append((cls.__name__, list(symbols)))

    async def subscribe_candle(self, symbols, interval, start_time=None, extended_trading_hours=False):
        self.subscribed.append(("Candle", list(symbols), interval))

    async def unsubscribe_candle(self, symbols, interval):
        pass

    async def listen(self, cls):
        q = self._q(cls)
        while True:
            yield await q.get()

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_quote_and_trade_from_cache(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Quote, Trade
    task = asyncio.create_task(_push_later(fake, Quote, SimpleNamespace(
        event_symbol="QQQ", bid_price=500.0, ask_price=500.2)))
    q = await s.quote("QQQ", timeout=2.0)
    assert q == {"bid": 500.0, "ask": 500.2}
    assert ("Quote", ["QQQ"]) in fake.subscribed
    task2 = asyncio.create_task(_push_later(fake, Trade, SimpleNamespace(
        event_symbol="QQQ", price=500.1)))
    assert await s.trade_price("QQQ", timeout=2.0) == 500.1
    await task; await task2; await s.teardown()


@pytest.mark.asyncio
async def test_candles_collected_and_normalized(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Candle
    t0 = datetime(2026, 7, 8, 14, 30, tzinfo=timezone.utc)
    ms = int(t0.timestamp() * 1000)
    ev = SimpleNamespace(event_symbol="QQQ{=5m}", time=ms, open=1.0, high=2.0,
                         low=0.5, close=1.5, volume=100)
    task = asyncio.create_task(_push_later(fake, Candle, ev))
    bars = await s.candles("QQQ", "5m", start=t0 - timedelta(hours=1), idle_seconds=0.5)
    await task; await s.teardown()
    assert bars == [{"time": ms, "open": 1.0, "high": 2.0, "low": 0.5,
                     "close": 1.5, "volume": 100.0, "symbol": "QQQ"}]


def _fake_connect(fake, s):
    async def _c():
        s._dx = fake
        s._start_pumps()
        return True
    return _c


async def _push_later(fake, cls, ev):
    await asyncio.sleep(0.05)
    await fake._q(cls).put(ev)


@pytest.mark.asyncio
async def test_candles_match_dxfeed_normalized_symbol(monkeypatch):
    """dxfeed echoes "QQQ{=d,tho=true}" for a "1d" subscription (observed live 2026-07-15);
    collection must match by prefix, and a forming candle with close=0.0 is dropped."""
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Candle
    t0 = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    ms = int(t0.timestamp() * 1000)
    good = SimpleNamespace(event_symbol="QQQ{=d,tho=true}", time=ms, open=1.0, high=2.0,
                           low=0.5, close=719.65, volume=100)
    forming = SimpleNamespace(event_symbol="QQQ{=d,tho=true}", time=ms + 86_400_000,
                              open=1.0, high=2.0, low=0.5, close=0.0, volume=0)
    task = asyncio.create_task(_push_later(fake, Candle, good))
    task2 = asyncio.create_task(_push_later(fake, Candle, forming))
    bars = await s.candles("QQQ", "1d", start=t0 - timedelta(days=3), idle_seconds=0.5)
    await task; await task2; await s.teardown()
    assert [b["close"] for b in bars] == [719.65]      # normalized symbol matched, 0.0 dropped
