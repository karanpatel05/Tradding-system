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
