"""Chart screen data: candle history and watchlist quotes degrade, validate and cache."""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import chart_data, minute_store


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    async def no_saved_minute(symbol):
        return None

    monkeypatch.setattr(chart_data, "minute_store", SimpleNamespace(latest_minute=no_saved_minute))
    chart_data._bars_cache.clear()
    chart_data._bars_locks.clear()
    yield
    chart_data._bars_cache.clear()
    chart_data._bars_locks.clear()


def _bar(t, close, **extra):
    return {"time": t, "open": close - 1, "high": close + 1, "low": close - 2,
            "close": close, "volume": 100, **extra}


@pytest.mark.asyncio
async def test_bars_are_cleaned_sorted_and_labelled(monkeypatch):
    async def bars(symbol, interval, start, end=None):
        assert (symbol, interval) == ("QQQ", "5m")
        return [_bar(2, 501.0), _bar(1, 500.0, symbol="QQQ"), _bar(3, 0.0), {"time": 4}]

    monkeypatch.setattr(chart_data, "facade_bars", bars)
    out = await chart_data.get_bars("qqq", "5m")
    assert out["source"] == "tastytrade" and out["note"] is None
    assert [b["time"] for b in out["bars"]] == [1, 2]
    assert set(out["bars"][0]) == {"time", "open", "high", "low", "close", "volume"}


@pytest.mark.asyncio
async def test_bars_are_cached_briefly(monkeypatch):
    calls = []

    async def bars(symbol, interval, start, end=None):
        calls.append(symbol)
        return [_bar(1, 500.0)]

    monkeypatch.setattr(chart_data, "facade_bars", bars)
    await chart_data.get_bars("SPY", "1m", now=100.0)
    await chart_data.get_bars("SPY", "1m", now=110.0)
    assert len(calls) == 1
    await chart_data.get_bars("SPY", "1m", now=110.0, recover=True)
    await chart_data.get_bars("SPY", "1m", now=110.5, recover=True)
    assert len(calls) == 2
    await chart_data.get_bars("SPY", "1m", now=110.0 + chart_data.BARS_TTL_SECONDS + 1)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_feed_failure_degrades_and_is_not_cached(monkeypatch):
    calls = []

    async def bars(symbol, interval, start, end=None):
        calls.append(symbol)
        raise RuntimeError("streamer down")

    monkeypatch.setattr(chart_data, "facade_bars", bars)
    out = await chart_data.get_bars("IWM", "15m")
    assert out["bars"] == [] and out["source"] is None and out["note"]
    await chart_data.get_bars("IWM", "15m")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_empty_feed_has_note(monkeypatch):
    async def bars(symbol, interval, start, end=None):
        return []

    monkeypatch.setattr(chart_data, "facade_bars", bars)
    out = await chart_data.get_bars("QQQ", "1d")
    assert out["bars"] == [] and "No candle data" in out["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,interval", [("QQQ", "1h"), ("../etc", "5m"), ("", "5m"),
                                             ("TOOLONGSYMBOL", "5m")])
async def test_bad_requests_are_rejected(symbol, interval):
    with pytest.raises(chart_data.ChartDataError):
        await chart_data.get_bars(symbol, interval)


@pytest.mark.asyncio
async def test_quotes_compute_change_and_flag_missing_data(monkeypatch):
    async def quotes(symbols, timeout=2.5):
        return {"QQQ": {"price": 505.0, "close": 500.0, "bid": 504.9, "ask": 505.1},
                "SPY": {"price": None, "close": None}}

    monkeypatch.setattr(chart_data, "facade_stock_quotes", quotes)
    out = await chart_data.get_quotes("qqq, spy,QQQ")
    assert list(out["quotes"]) == ["QQQ", "SPY"]
    assert out["quotes"]["QQQ"]["change_pct"] == pytest.approx(1.0)
    assert out["quotes"]["SPY"]["change_pct"] is None
    assert out["data_available"] is True


@pytest.mark.asyncio
async def test_quotes_degrade_when_feed_fails(monkeypatch):
    async def quotes(symbols, timeout=2.5):
        raise RuntimeError("down")

    monkeypatch.setattr(chart_data, "facade_stock_quotes", quotes)
    out = await chart_data.get_quotes("QQQ")
    assert out["data_available"] is False and out["note"]


@pytest.mark.asyncio
async def test_quotes_reject_bad_lists():
    with pytest.raises(chart_data.ChartDataError):
        await chart_data.get_quotes("")
    with pytest.raises(chart_data.ChartDataError):
        await chart_data.get_quotes(",".join(f"S{i}" for i in range(25)))


@pytest.mark.asyncio
async def test_empty_result_is_not_cached(monkeypatch):
    """An empty answer (feed still reconnecting) must not pin the chart to sample data
    for the cache TTL: the next poll asks the feed again."""
    answers = [[], [_bar(1, 500.0)]]

    async def bars(symbol, interval, start, end=None):
        return answers.pop(0)

    monkeypatch.setattr(chart_data, "facade_bars", bars)
    first = await chart_data.get_bars("QQQ", "5m", now=100.0)
    second = await chart_data.get_bars("QQQ", "5m", now=101.0)
    assert first["bars"] == [] and len(second["bars"]) == 1


@pytest.mark.asyncio
async def test_public_minutes_after_restart_and_regular_only_chart_fallback(tmp_path, monkeypatch):
    from app.routers.dashboard import router

    url = f"sqlite+aiosqlite:///{tmp_path / 'saved.db'}"

    def open_store():
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine)

        @asynccontextmanager
        async def scope():
            async with sessions.begin() as session:
                yield session

        monkeypatch.setattr(minute_store, "engine", engine)
        monkeypatch.setattr(minute_store, "session_scope", scope)
        return engine

    def row(day, hour, close):
        t = datetime.fromisoformat(f"2026-11-{day}T{hour}:00-05:00")
        return _bar(int(t.timestamp() * 1000), close, volume=10.5)

    # The final two prints on November 27 are after the 13:00 early close.
    previous = row(25, "19:59", 100)
    today = [row(27, hm, 101 + i) for i, hm in enumerate(["04:15", "12:59", "13:00", "19:59"])]
    writer = open_store()
    await minute_store.init_archive()
    await minute_store.save_minutes("SPY", [previous, *today])
    await writer.dispose()

    reader = open_store()
    monkeypatch.setattr(chart_data, "minute_store", minute_store)

    async def offline(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(chart_data, "facade_bars", offline)
    app = FastAPI()
    app.include_router(router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            latest = (await client.get("/api/market/minutes", params={"symbol": "spy"})).json()
            assert (latest["day"], latest["source"], latest["bars"]) == ("2026-11-27", "archive", today)
            assert latest["regular_close_ms"] == today[2]["time"]
            earlier = await client.get("/api/market/minutes", params={"symbol": "SPY", "day": "2026-11-25"})
            assert earlier.json()["bars"] == [previous]
            missing = await client.get("/api/market/minutes", params={"symbol": "ABC"})
            assert missing.json()["bars"] == [] and missing.json()["day"] is None
            fallback = (await client.get("/api/market/bars", params={"symbol": "SPY", "interval": "1m"})).json()
            assert fallback["source"] == "archive" and fallback["bars"] == [today[1]]
            assert (await client.get("/api/market/minutes", params={"symbol": "../bad"})).status_code == 400
            assert (await client.get("/api/market/minutes", params={"symbol": "SPY", "day": "bad"})).status_code == 422
    finally:
        await reader.dispose()


@pytest.mark.asyncio
async def test_quotes_report_market_session(monkeypatch):
    """The chart folds live quotes into its newest candle only during the regular session,
    so the quotes response carries the server's session (it knows NYSE holidays)."""
    async def quotes(symbols, timeout=2.5):
        return {"QQQ": {"price": 505.0, "close": 500.0}}

    monkeypatch.setattr(chart_data, "facade_stock_quotes", quotes)
    monkeypatch.setattr(chart_data, "get_market_status", lambda: {"session": "regular"})
    assert (await chart_data.get_quotes("QQQ"))["session"] == "regular"

    async def down(symbols, timeout=2.5):
        raise RuntimeError("down")

    monkeypatch.setattr(chart_data, "facade_stock_quotes", down)
    monkeypatch.setattr(chart_data, "get_market_status", lambda: {"session": "closed"})
    assert (await chart_data.get_quotes("QQQ"))["session"] == "closed"
