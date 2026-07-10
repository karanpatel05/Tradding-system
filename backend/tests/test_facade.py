"""Facade ordering: TT ok -> TT serves; TT fails + Gateway READY -> IBKR serves;
TT fails + not READY -> None; data_primary='ibkr' reverses. Sanity guard wired into
option_mid for both sources. All sources faked - no network."""
import pytest

from app.config import get_settings
from app.marketdata import facade


class _FakeStreamer:
    def __init__(self, quote=None, trade=None):
        self._q, self._t = quote, trade
    async def quote(self, symbol, timeout=3.0):
        return self._q
    async def trade_price(self, symbol, timeout=3.0):
        return self._t


def _reset():
    facade._last_source.clear()
    facade._journal_cooldown.clear()
    facade._chains.clear()
    facade._sanity._last.clear()


@pytest.mark.asyncio
async def test_tt_serves_when_available(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    monkeypatch.setattr(facade, "_get_streamer",
                        lambda: _FakeStreamer(quote={"bid": 500.0, "ask": 500.2}, trade=500.1))
    q = await facade.stock_quote("QQQ")
    assert q["price"] == 500.1 and q["bid"] == 500.0


@pytest.mark.asyncio
async def test_fallback_to_ibkr_when_ready(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_get_streamer", lambda: _FakeStreamer())  # TT empty
    monkeypatch.setattr(facade, "_ib_ready", lambda: True)
    async def fake_ib(symbols, timeout=3.0):
        return {"QQQ": {"bid": 1.0, "ask": 1.2, "last": 1.1, "close": None, "price": 1.1}}
    monkeypatch.setattr(facade, "_ib_stock_quotes", fake_ib)
    q = await facade.stock_quote("QQQ")
    assert q["price"] == 1.1


@pytest.mark.asyncio
async def test_both_down_returns_none(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_get_streamer", lambda: _FakeStreamer())
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    assert await facade.stock_quote("QQQ") is None


@pytest.mark.asyncio
async def test_option_mid_sanity_rejects_jump(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    monkeypatch.setattr(facade, "_tt_chain", _fake_chain)
    good = _FakeStreamer(quote={"bid": 3.9, "ask": 4.1})
    monkeypatch.setattr(facade, "_get_streamer", lambda: good)
    from datetime import date
    e = date(2026, 7, 30)
    assert await facade.option_mid("QQQ", e, 500.0, "C") == pytest.approx(4.0)
    bad = _FakeStreamer(quote={"bid": 57.0, "ask": 59.0})   # jump vs accepted 4.0
    monkeypatch.setattr(facade, "_get_streamer", lambda: bad)
    assert await facade.option_mid("QQQ", e, 500.0, "C") is None


async def _fake_chain(underlying):
    return {"expiries": ["2026-07-30"], "strikes": [495.0, 500.0, 505.0],
            "by_key": {("2026-07-30", 500.0, "C"): ".QQQ260730C500"}}


class _PerSymbolStreamer:
    """Serves fixed quote/trade data per symbol; missing symbols are misses."""
    def __init__(self, quotes: dict, trades: dict):
        self._quotes, self._trades = quotes, trades
    async def quote(self, symbol, timeout=3.0):
        return self._quotes.get(symbol)
    async def trade_price(self, symbol, timeout=3.0):
        return self._trades.get(symbol)


@pytest.mark.asyncio
async def test_no_false_transition_across_symbols(monkeypatch):
    """Symbol A stays on tastytrade throughout; symbol B falls back to ibkr on its
    own first call. Because _last_source is keyed per-instrument, A's steady state
    must never be reported as a transition just because B (a different instrument)
    is being served by a different source."""
    _reset()
    monkeypatch.setattr(facade, "_ib_ready", lambda: True)
    streamer = _PerSymbolStreamer(quotes={"A": {"bid": 10.0, "ask": 10.2}},
                                  trades={"A": 10.1})
    monkeypatch.setattr(facade, "_get_streamer", lambda: streamer)

    async def fake_ib(symbols, timeout=3.0):
        return {"B": {"bid": 20.0, "ask": 20.2, "last": 20.1, "close": None, "price": 20.1}}
    monkeypatch.setattr(facade, "_ib_stock_quotes", fake_ib)

    calls = []
    async def fake_log_event(message, **kwargs):
        calls.append(message)
    monkeypatch.setattr("app.services.journal.log_event", fake_log_event)

    await facade.stock_quote("A")
    await facade.stock_quote("B")
    await facade.stock_quote("A")

    assert not any("now served by" in c for c in calls)


@pytest.mark.asyncio
async def test_null_price_quote_falls_back(monkeypatch):
    """A quote with bid=None/ask=None and no trade price yields price=None from the
    tastytrade closure, which must be treated as a miss so _race falls through to
    IBKR rather than returning a price-less dict."""
    _reset()
    monkeypatch.setattr(facade, "_ib_ready", lambda: True)
    monkeypatch.setattr(facade, "_get_streamer",
                        lambda: _FakeStreamer(quote={"bid": None, "ask": None}, trade=None))

    async def fake_ib(symbols, timeout=3.0):
        return {"QQQ": {"bid": 5.0, "ask": 5.2, "last": 5.1, "close": None, "price": 5.1}}
    monkeypatch.setattr(facade, "_ib_stock_quotes", fake_ib)

    q = await facade.stock_quote("QQQ")
    assert q["price"] == 5.1
