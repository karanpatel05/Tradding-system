"""Facade ordering: TT ok -> TT serves; TT miss -> None (Tastytrade is the sole
data source; there is no IBKR fallback). Sanity guard wired into option_mid.
All sources faked - no network."""
import pytest

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
    monkeypatch.setattr(facade, "_get_streamer",
                        lambda: _FakeStreamer(quote={"bid": 500.0, "ask": 500.2}, trade=500.1))
    q = await facade.stock_quote("QQQ")
    assert q["price"] == 500.1 and q["bid"] == 500.0


@pytest.mark.asyncio
async def test_tt_miss_returns_none(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_get_streamer", lambda: _FakeStreamer())  # TT empty
    assert await facade.stock_quote("QQQ") is None


@pytest.mark.asyncio
async def test_option_mid_sanity_rejects_jump(monkeypatch):
    _reset()
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
async def test_no_false_transition_within_same_source(monkeypatch):
    """Symbol A is served by tastytrade on every call. Repeated calls for the same
    (steady-state) source must never be journaled as a transition."""
    _reset()
    streamer = _PerSymbolStreamer(quotes={"A": {"bid": 10.0, "ask": 10.2}},
                                  trades={"A": 10.1})
    monkeypatch.setattr(facade, "_get_streamer", lambda: streamer)

    calls = []
    async def fake_log_event(message, **kwargs):
        calls.append(message)
    monkeypatch.setattr("app.services.journal.log_event", fake_log_event)

    await facade.stock_quote("A")
    await facade.stock_quote("A")
    await facade.stock_quote("A")

    assert not any("now served by" in c for c in calls)


@pytest.mark.asyncio
async def test_transition_journaling_is_per_instrument_and_cooldown(monkeypatch):
    """_note_source only journals on an actual source change for a given
    instrument-scoped op key; a first-time recording and repeats of the same
    source are not transitions. Exercised directly (source names are arbitrary
    labels here) so this stays meaningful even with a single live data source."""
    _reset()
    calls = []
    async def fake_log_event(message, **kwargs):
        calls.append(message)
    monkeypatch.setattr("app.services.journal.log_event", fake_log_event)

    await facade._note_source("stock_quote:A", "tastytrade")
    await facade._note_source("stock_quote:B", "tastytrade")
    await facade._note_source("stock_quote:A", "tastytrade")
    assert not calls  # first-time recording + repeats of the same source: no journal

    await facade._note_source("stock_quote:A", "other")
    assert calls and "now served by other" in calls[0]


@pytest.mark.asyncio
async def test_null_price_quote_is_a_miss(monkeypatch):
    """A quote with bid=None/ask=None and no trade price yields price=None from the
    tastytrade closure, which must be treated as a miss (None), not a price-less dict."""
    _reset()
    monkeypatch.setattr(facade, "_get_streamer",
                        lambda: _FakeStreamer(quote={"bid": None, "ask": None}, trade=None))
    assert await facade.stock_quote("QQQ") is None
