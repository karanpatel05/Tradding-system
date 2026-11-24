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

    async def unsubscribe(self, cls, symbols):
        self.subscribed.append(("unsub", cls.__name__, list(symbols)))

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


# --- reconnect after the websocket dies --------------------------------------------
# The real SDK runs its reader inside an anyio task group bound to whichever task
# entered the context. When the socket drops, the reader raises inside that group;
# nothing reaches our pumps (they block on in-memory queues forever). The fake below
# reproduces that structure so the streamer must notice the death itself.
from contextlib import asynccontextmanager  # noqa: E402

import anyio  # noqa: E402


class _FakeSdk:
    instances: list["_FakeSdk"] = []

    def __init__(self, session):
        self.dx = _FakeDx()
        self.kill = asyncio.Event()
        _FakeSdk.instances.append(self)

    @asynccontextmanager
    async def _cm(self):
        async with anyio.create_task_group() as tg:
            async def reader():
                await self.kill.wait()
                raise ConnectionError("socket closed by server")
            tg.start_soon(reader)
            yield self.dx
            tg.cancel_scope.cancel()

    async def __aenter__(self):
        self._ctx = self._cm()
        return await self._ctx.__aenter__()

    async def __aexit__(self, *exc):
        return await self._ctx.__aexit__(*exc)


async def _fake_session():
    return object()


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeSdk.instances = []
    monkeypatch.setattr(st_mod, "DXLinkStreamer", _FakeSdk)
    monkeypatch.setattr(st_mod, "get_tt_session", _fake_session)
    return _FakeSdk


async def _quote_in_request(s, fake, symbol, bid):
    """First use from a short-lived task, like an HTTP request handler."""
    from tastytrade.dxfeed import Quote
    push = asyncio.create_task(_push_later(fake.dx, Quote, SimpleNamespace(
        event_symbol=symbol, bid_price=bid, ask_price=bid + 0.1)))
    q = await asyncio.create_task(s.quote(symbol, timeout=2.0))
    await push
    return q


@pytest.mark.asyncio
async def test_reconnects_after_socket_dies(fake_sdk):
    s = st_mod.TTStreamer()
    await asyncio.create_task(s._ensure())          # connect from a finished "request"
    assert await _quote_in_request(s, fake_sdk.instances[0], "SPY", 500.0) == {
        "bid": 500.0, "ask": 500.1}

    fake_sdk.instances[0].kill.set()                # server drops the websocket
    for _ in range(50):
        if s._dx is None:
            break
        await asyncio.sleep(0.02)
    assert s._dx is None, "dead connection must be dropped, not kept as live"

    await asyncio.create_task(s._ensure())          # next read reconnects
    assert len(fake_sdk.instances) == 2
    assert await _quote_in_request(s, fake_sdk.instances[1], "QQQ", 600.0) == {
        "bid": 600.0, "ask": 600.1}
    await s.teardown()


@pytest.mark.asyncio
async def test_failed_subscribe_drops_connection(fake_sdk):
    s = st_mod.TTStreamer()
    assert await s._ensure()

    async def broken(cls, symbols):
        raise RuntimeError("LocalProtocolError: connection closed")
    fake_sdk.instances[0].dx.subscribe = broken

    assert await s.quote("SPY", timeout=0.2) is None
    assert s._dx is None, "a subscribe failure must force a reconnect on the next read"
    await s.teardown()


@pytest.mark.asyncio
async def test_connect_failure_returns_false_and_retries(monkeypatch):
    calls = []

    class _Boom(_FakeSdk):
        async def __aenter__(self):
            calls.append(1)
            raise OSError("nodename nor servname provided")
    monkeypatch.setattr(st_mod, "DXLinkStreamer", _Boom)
    monkeypatch.setattr(st_mod, "get_tt_session", _fake_session)
    s = st_mod.TTStreamer()
    assert await s._ensure() is False
    assert await s._ensure() is False
    assert len(calls) == 2 and s._dx is None


@pytest.mark.asyncio
async def test_concurrent_subscribes_are_serialized(fake_sdk):
    """SDK 12.x race: a 2nd subscribe on a channel can send FEED_SUBSCRIPTION before the
    1st has sent FEED_SETUP; the server then replies in FULL format and the SDK reader
    dies (KeyError: 0). The overview subscribes 5 symbols at once, so subscribes must
    never overlap."""
    s = st_mod.TTStreamer()
    assert await s._ensure()
    dx = fake_sdk.instances[0].dx
    active = {"now": 0, "max": 0}

    async def slow_subscribe(cls, symbols):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)                  # channel request / feed setup in flight
        active["now"] -= 1
        dx.subscribed.append((cls.__name__, list(symbols)))
    dx.subscribe = slow_subscribe

    syms = ["SPY", "QQQ", "DIA", "IWM", "VIXY"]
    await asyncio.gather(*[s.quote(x, timeout=0.05) for x in syms],
                         *[s.trade_price(x, timeout=0.05) for x in syms])
    assert active["max"] == 1
    assert len(dx.subscribed) == 10
    await s.teardown()


def test_trade_with_fractional_day_volume_is_not_dropped():
    """Live 2026-11-19: Tastytrade sends fractional dayVolume (19189452.77). SDK 12.4.1
    types it int, so every Trade failed validation and was silently skipped: the board's
    'last' froze at the previous session's trade. Importing the streamer relaxes it."""
    import app.tastytrade.streamer  # noqa: F401 - applies the model patch
    from tastytrade.dxfeed import Quote, Trade
    row = ['SPY', 0, 1795105424454, 0, 48297, 'Q', 20776, 'ZERO_DOWN', False, 728.63,
           -1.57, 80.5, 19189452.774271, 14099538605.52173]
    [t] = Trade.from_stream(row)
    assert float(t.price) == 728.63 and t.day_volume == 19189452.774271
    q = dict.fromkeys(Quote.model_fields, 0)
    q.update(event_symbol="SPY", bid_price=1.0, ask_price=1.1, bid_size=10.5, ask_size=0.25,
             bid_exchange_code="Q", ask_exchange_code="Q")
    assert Quote.model_validate(q).bid_size == 10.5


@pytest.mark.asyncio
async def test_trade_change_gives_previous_close(monkeypatch):
    """dxfeed Trade carries `change` vs the previous session's close (live: price 728.63,
    change -1.57, Summary prevDayClosePrice 730.2). prev_close = price - change."""
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Trade
    push = asyncio.create_task(_push_later(fake, Trade, SimpleNamespace(
        event_symbol="SPY", price=728.63, change=-1.57)))
    assert await s.trade_price("SPY", timeout=2.0) == 728.63
    await push
    assert s.prev_close("SPY") == 730.2
    assert s.prev_close("QQQ") is None
    await s.teardown()


@pytest.mark.asyncio
async def test_candles_wait_for_a_slow_first_event(monkeypatch):
    """Right after a (re)connect the first candle can take longer than two idle periods.
    Collection must keep waiting for the first event instead of returning []."""
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Candle
    t0 = datetime(2026, 7, 8, 14, 30, tzinfo=timezone.utc)
    ms = int(t0.timestamp() * 1000)
    ev = SimpleNamespace(event_symbol="QQQ{=5m}", time=ms, open=1.0, high=2.0,
                         low=0.5, close=1.5, volume=100)

    async def push_slow():
        await asyncio.sleep(0.7)  # > 2 x idle_seconds
        await fake._q(Candle).put(ev)

    task = asyncio.create_task(push_slow())
    bars = await s.candles("QQQ", "5m", start=t0 - timedelta(hours=1), idle_seconds=0.2,
                           first_event_timeout=3.0)
    await task; await s.teardown()
    assert [b["close"] for b in bars] == [1.5]


@pytest.mark.asyncio
async def test_candles_give_up_after_first_event_timeout(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    loop = asyncio.get_running_loop()
    t = loop.time()
    bars = await s.candles("QQQ", "5m", start=datetime.now(timezone.utc) - timedelta(hours=1),
                           idle_seconds=0.1, first_event_timeout=0.5)
    await s.teardown()
    assert bars == [] and 0.4 < loop.time() - t < 1.5


@pytest.mark.asyncio
async def test_candles_stop_at_max_seconds_even_if_events_keep_coming(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Candle
    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
    base = int(t0.timestamp() * 1000)

    async def flood():
        for i in range(200):
            await fake._q(Candle).put(SimpleNamespace(
                event_symbol="QQQ{=1m}", time=base + i * 60_000, open=1.0, high=2.0,
                low=0.5, close=1.5, volume=1))
            await asyncio.sleep(0.02)

    task = asyncio.create_task(flood())
    loop = asyncio.get_running_loop()
    t = loop.time()
    bars = await s.candles("QQQ", "1m", start=t0, idle_seconds=0.1, max_seconds=0.6)
    elapsed = loop.time() - t
    task.cancel(); await s.teardown()
    assert bars and elapsed < 1.2


# --- TimeAndSale (live tick recorder) -----------------------------------------------
def _tas_event(symbol="SPY", index=1):
    return SimpleNamespace(event_symbol=symbol, time=1795105424454, index=index, price=1.0,
                           size=1.0, valid_tick=True, extended_trading_hours=False, type="NEW")


@pytest.mark.asyncio
async def test_time_and_sale_listener_receives_events(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import TimeAndSale
    got = []

    def boom(ev):
        raise RuntimeError("listener bug")
    s.add_listener("TimeAndSale", boom)
    s.add_listener("TimeAndSale", got.append)
    s.add_listener("TimeAndSale", got.append)            # idempotent
    assert await s._ensure()
    assert await s._subscribe(TimeAndSale, "SPY")
    assert "SPY" in s._subscribed["TimeAndSale"]
    await fake._q(TimeAndSale).put(_tas_event(index=1))
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    assert [e.index for e in got] == [1]                 # called once per event
    await fake._q(TimeAndSale).put(_tas_event(index=2))  # a raising listener did not kill it
    for _ in range(50):
        if len(got) == 2:
            break
        await asyncio.sleep(0.01)
    assert [e.index for e in got] == [1, 2] and s._dx is fake
    s.remove_listener("TimeAndSale", got.append)
    s.remove_listener("TimeAndSale", got.append)         # missing: no error
    await s.teardown()
    assert s._subscribed["TimeAndSale"] == set()         # cleared on drop, key kept


@pytest.mark.asyncio
async def test_unsubscribe_is_best_effort(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import TimeAndSale
    assert await s._ensure()
    await s._subscribe(TimeAndSale, "SPY")
    await s._subscribe(TimeAndSale, "QQQ")
    await s._unsubscribe(TimeAndSale, ["SPY"])
    assert s._subscribed["TimeAndSale"] == {"QQQ"}
    assert ("unsub", "TimeAndSale", ["SPY"]) in fake.subscribed

    async def broken(cls, symbols):
        raise RuntimeError("socket closed")
    fake.unsubscribe = broken
    await s._unsubscribe(TimeAndSale, ["QQQ"])           # never raises, never tears down
    assert s._subscribed["TimeAndSale"] == set() and s._dx is fake
    await s.teardown()


def test_time_and_sale_with_fractional_size_is_not_dropped():
    import app.tastytrade.streamer  # noqa: F401 - applies the model patch
    from tastytrade.dxfeed import TimeAndSale
    row = ["NVDA", 0, 0, 7406, 1795105424454, 0, 1, "Q", 180.25, 10.5, 180.2, 180.3,
           "@ I", "0", "BUY", False, False, False, "NEW", None, None]
    [t] = TimeAndSale.from_stream(row)
    assert t.size == 10.5 and t.index == 7406
