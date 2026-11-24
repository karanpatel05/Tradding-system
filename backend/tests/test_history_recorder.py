"""LiveRecorder against a fake streamer, an injected clock and a temp store. No network."""
import asyncio
from contextlib import suppress
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.history.live_bars import ET, Trade, stock_symbols
from app.history.recorder import LiveRecorder
from app.history.store import HistoryStore

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
DAY = date(2026, 11, 23)          # a Monday, not a holiday


def _dt(h, m, s):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=ET).astimezone(UTC)


def _et(h, m, s, ms=0):
    return int(_dt(h, m, s).timestamp() * 1000) + ms


def _tas(symbol, t_ms, index, price, size, extended=False, valid=True, type_="NEW"):
    return SimpleNamespace(event_symbol=symbol, time=t_ms, index=index, price=price,
                           size=size, valid_tick=valid, extended_trading_hours=extended,
                           type=type_)


def _raise(exc):
    def f(*a, **kw):
        raise exc
    return f


class FakeStreamer:
    def __init__(self):
        self._subscribed = {"Quote": set(), "Trade": set(), "TimeAndSale": set()}
        self.listeners: dict[str, list] = {}
        self.subscribe_calls = 0

    async def _ensure(self):
        return True

    async def _subscribe(self, cls, symbol):
        self.subscribe_calls += 1
        self._subscribed[cls.__name__].add(symbol)
        return True

    async def _unsubscribe(self, cls, symbols):
        for s in symbols:
            self._subscribed[cls.__name__].discard(s)

    def add_listener(self, name, fn):
        lst = self.listeners.setdefault(name, [])
        if fn not in lst:
            lst.append(fn)

    def remove_listener(self, name, fn):
        with suppress(ValueError):
            self.listeners.get(name, []).remove(fn)


@pytest.fixture
def store(tmp_path):
    return HistoryStore(str(tmp_path / "h.db"))


@pytest.fixture
def fake_streamer():
    return FakeStreamer()


async def _until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.005)


async def _stop(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class Clock:
    """A settable clock that counts reads, so a test can wait for loop iterations."""
    def __init__(self, t):
        self.t, self.reads = t, 0

    def __call__(self):
        self.reads += 1
        return self.t

    async def ticks(self, n):
        start = self.reads
        await _until(lambda: self.reads >= start + n)


async def test_recorder_records_rth_trades_and_flushes(store, fake_streamer):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30))
    rec.on_event(_tas("SPY", _et(10, 0, 1), 1, 100.0, 10))
    rec.on_event(_tas("SPY", _et(10, 0, 2), 2, 100.0, 1, extended=True))     # dropped
    rec.on_event(_tas("AAPL", _et(10, 0, 2), 1, 1.0, 1))                     # not listed
    assert await rec.flush() == 1
    assert store.live_minutes("SPY", 0, 2**62)[0]["volume"] == 10.0
    assert rec.live_bars("SPY")[-1]["close"] == 100.0


async def test_flush_failure_keeps_buffer_and_continues(store, fake_streamer, monkeypatch):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30))
    rec.on_event(_tas("SPY", _et(10, 0, 1), 1, 100.0, 10))
    monkeypatch.setattr(store, "insert_trades", _raise(OSError("disk full")))
    assert await rec.flush() == 0
    rec.on_event(_tas("SPY", _et(10, 0, 2), 2, 101.0, 5))      # arrives while failing
    monkeypatch.undo()
    assert await rec.flush() == 2
    assert store.live_minutes("SPY", 0, 2**62)[0]["volume"] == 15.0


async def test_odd_lots_count_and_type_filter(store, fake_streamer):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30))
    rec.on_event(_tas("SPY", _et(10, 0, 1), 1, 100.0, 100))
    rec.on_event(_tas("SPY", _et(10, 0, 2), 2, 105.0, 3, valid=False))      # odd lot
    rec.on_event(_tas("SPY", _et(10, 0, 3), 3, 101.0, 7, type_="CORRECTION"))
    rec.on_event(_tas("SPY", _et(10, 0, 4), 4, 999.0, 7, type_="DELETE"))    # dropped
    rec.on_event(_tas("SPY", _et(10, 0, 5), 5, float("nan"), 7))             # dropped
    rec.on_event(_tas("SPY", _et(9, 29, 59), 6, 98.0, 7))                    # pre-market
    rec.on_event(_tas("QQQ", _et(16, 0, 0), 1, 50.0, 2))                     # closing print
    assert await rec.flush() == 4
    [bar] = store.live_minutes("SPY", 0, 2**62)
    assert bar["volume"] == 110.0 and bar["high"] == 101.0 and bar["close"] == 101.0
    assert rec.live_bars("SPY") == [{"time": _et(10, 0, 0), "open": 100.0, "high": 101.0,
                                     "low": 100.0, "close": 101.0, "volume": 110.0}]
    assert store.live_minutes("QQQ", 0, 2**62)[0]["time"] == _et(15, 59, 0)


async def test_live_bars_window_and_pruning(store, fake_streamer):
    clock = Clock(_dt(10, 2, 30))
    rec = LiveRecorder(store, lambda: fake_streamer, now=clock)
    for i, m in enumerate((0, 1, 2)):
        rec.on_event(_tas("SPY", _et(10, m, 1), i, 100.0 + m, 1))
    assert [b["time"] for b in rec.live_bars("SPY")] == [_et(10, 1, 0), _et(10, 2, 0)]
    await rec.flush()
    assert sorted(rec._bars["SPY"]) == [_et(10, 1, 0), _et(10, 2, 0)]     # 10:00 pruned
    clock.t = _dt(16, 0, 30)
    assert rec.live_bars("SPY") == []


async def test_recorder_resubscribes_after_drop(store, fake_streamer):
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30),
                       flush_seconds=0.01)
    task = asyncio.create_task(rec.run())
    syms = set(stock_symbols())
    await _until(lambda: syms <= fake_streamer._subscribed["TimeAndSale"])
    fake_streamer._subscribed["TimeAndSale"].clear()                         # DXLink drop
    await _until(lambda: syms <= fake_streamer._subscribed["TimeAndSale"])
    await _stop(task)
    assert set(stock_symbols()) <= fake_streamer._subscribed["TimeAndSale"]
    assert fake_streamer.listeners["TimeAndSale"] == []                      # removed on stop


async def test_no_subscription_outside_rth(store, fake_streamer):
    clock = Clock(_dt(17, 0, 0))
    rec = LiveRecorder(store, lambda: fake_streamer, now=clock, flush_seconds=0.01)
    task = asyncio.create_task(rec.run())
    await clock.ticks(1)
    await asyncio.sleep(0.05)
    await _stop(task)
    assert not fake_streamer._subscribed["TimeAndSale"]
    assert fake_streamer.subscribe_calls == 0


async def test_unsubscribes_when_rth_ends(store, fake_streamer):
    clock = Clock(_dt(15, 59, 59))
    rec = LiveRecorder(store, lambda: fake_streamer, now=clock, flush_seconds=0.01)
    task = asyncio.create_task(rec.run())
    await _until(lambda: fake_streamer._subscribed["TimeAndSale"])
    clock.t = _dt(16, 0, 30)
    await clock.ticks(5)
    assert fake_streamer._subscribed["TimeAndSale"]                         # grace for the closing print
    clock.t = _dt(16, 1, 1)
    await _until(lambda: not fake_streamer._subscribed["TimeAndSale"])
    await _stop(task)
    assert not fake_streamer._subscribed["TimeAndSale"]


async def test_run_seeds_from_store_and_flushes(store, fake_streamer):
    store.insert_trades([Trade("SPY", _et(10, 0, 1), 1, 100.0, 10.0),
                         Trade("SPY", _et(10, 0, 20), 2, 102.0, 5.0)])
    rec = LiveRecorder(store, lambda: fake_streamer, now=lambda: _dt(10, 0, 30),
                       flush_seconds=0.01)
    task = asyncio.create_task(rec.run())
    await _until(lambda: fake_streamer.listeners.get("TimeAndSale") == [rec.on_event])
    assert rec.live_bars("SPY") == [{"time": _et(10, 0, 0), "open": 100.0, "high": 102.0,
                                     "low": 100.0, "close": 102.0, "volume": 15.0}]
    fake_streamer.listeners["TimeAndSale"][0](_tas("SPY", _et(10, 0, 25), 3, 99.0, 1))
    await _until(lambda: store.live_minutes("SPY", 0, 2**62)[0]["volume"] == 16.0)
    assert rec.live_bars("SPY")[0]["low"] == 99.0 and rec.live_bars("SPY")[0]["open"] == 100.0
    await _stop(task)


async def test_run_survives_errors(store, fake_streamer):
    calls = []

    def flaky_factory():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("streamer unavailable")
        return fake_streamer
    rec = LiveRecorder(store, flaky_factory, now=lambda: _dt(10, 0, 30), flush_seconds=0.001)
    task = asyncio.create_task(rec.run())
    await _until(lambda: set(stock_symbols()) <= fake_streamer._subscribed["TimeAndSale"])
    assert not task.done()
    await _stop(task)
