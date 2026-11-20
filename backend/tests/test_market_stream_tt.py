"""Market stream over DXLink: ensure_started subscribes overview symbols; overview()
reads the streamer caches synchronously."""
import pytest

from app.services import market_stream as ms


@pytest.mark.asyncio
async def test_overview_reads_streamer_cache(monkeypatch):
    class _FakeStreamer:
        _quotes = {"SPY": {"bid": 500.0, "ask": 500.2}}
        _trades = {"SPY": 500.1}
        async def quote(self, symbol, timeout=3.0):
            return self._quotes.get(symbol)
        async def trade_price(self, symbol, timeout=3.0):
            return self._trades.get(symbol)
    monkeypatch.setattr(ms, "_streamer", lambda: _FakeStreamer())
    rows = ms.market_stream.overview()
    spy = next((r for r in rows if r["symbol"] == "SPY"), None)
    assert spy is not None and spy["last"] == 500.1


class _DropStreamer:
    """Fake shared streamer: `_dx` None = the DXLink socket dropped."""
    def __init__(self, connected):
        self._dx = object() if connected else None
        self._quotes, self._trades = {}, {}
        self.reads = []

    async def quote(self, sym):
        self.reads.append(("quote", sym))
        self._dx = object()                        # a read reconnects (TTStreamer._ensure)

    async def trade_price(self, sym):
        self.reads.append(("trade", sym))


@pytest.mark.asyncio
async def test_keep_alive_resubscribes_after_a_drop(monkeypatch):
    """The WS loop calls keep_alive() every tick. After a socket drop, it must re-read
    the overview symbols (which reconnects and resubscribes) instead of leaving the
    board on stale prices until the page is reloaded."""
    from app.services import market_stream as ms
    fake = _DropStreamer(connected=False)
    monkeypatch.setattr(ms, "_streamer", lambda: fake)
    stream = ms.MarketStream()
    stream.keep_alive()
    await stream._task
    assert ("quote", "SPY") in fake.reads and ("trade", "VIXY") in fake.reads


@pytest.mark.asyncio
async def test_keep_alive_is_a_noop_while_connected(monkeypatch):
    from app.services import market_stream as ms
    fake = _DropStreamer(connected=True)
    monkeypatch.setattr(ms, "_streamer", lambda: fake)
    stream = ms.MarketStream()
    stream.keep_alive()
    assert stream._task is None and fake.reads == []


def test_overview_day_change_from_prev_close(monkeypatch):
    class _S:
        _quotes = {"SPY": {"bid": 728.6, "ask": 728.7}}
        _trades = {"SPY": 728.63}
        _prev_close = {"SPY": 730.2}
    monkeypatch.setattr(ms, "_streamer", lambda: _S())
    spy = next(r for r in ms.market_stream.overview() if r["symbol"] == "SPY")
    assert spy["prev_close"] == 730.2
    assert spy["change"] == -1.57 and spy["change_pct"] == round(-1.57 / 730.2, 4)
