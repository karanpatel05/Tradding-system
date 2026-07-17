"""/api/builder/spot: last price + day change vs previous close, facade-served.
When the live quote lacks a close, the previous close comes from daily bars — the newest
bar is skipped when it is today's (still forming)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.routers import builder


@pytest.mark.asyncio
async def test_change_from_quote_close(monkeypatch):
    from app.marketdata import facade
    async def fake_quote(sym):
        return {"bid": None, "ask": None, "last": 725.5, "close": 723.28, "price": 725.5}
    monkeypatch.setattr(facade, "stock_quote", fake_quote)
    out = await builder.spot("qqq")
    assert out["underlying"] == "QQQ" and out["price"] == 725.5
    assert out["change_abs"] == pytest.approx(2.22)
    assert out["change_pct"] == pytest.approx(2.22 / 723.28, abs=1e-6)


@pytest.mark.asyncio
async def test_change_from_daily_bars_skips_forming_bar(monkeypatch):
    from app.marketdata import facade
    async def fake_quote(sym):
        return {"bid": None, "ask": None, "last": 100.0, "close": None, "price": 100.0}
    from zoneinfo import ZoneInfo
    # dxfeed-style stamps: a session's daily bar is stamped 20:00 ET the PRIOR day.
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    today_stamp = (now.replace(hour=20, minute=0, second=0, microsecond=0)
                   - timedelta(days=1))                  # today's session bar
    today_ms = int(today_stamp.timestamp() * 1000)
    yday_ms = int((today_stamp - timedelta(days=1)).timestamp() * 1000)
    async def fake_bars(sym, tf, start=None, end=None):
        return [{"time": yday_ms, "close": 98.0}, {"time": today_ms, "close": 99.5}]
    monkeypatch.setattr(facade, "stock_quote", fake_quote)
    monkeypatch.setattr(facade, "bars", fake_bars)
    out = await builder.spot("QQQ")
    # newest bar is today's forming candle -> prev close is yesterday's 98.0
    assert out["prev_close"] == 98.0
    assert out["change_abs"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_no_data_degrades(monkeypatch):
    from app.marketdata import facade
    async def none_quote(sym):
        return None
    monkeypatch.setattr(facade, "stock_quote", none_quote)
    out = await builder.spot("QQQ")
    assert out["price"] is None and out["change_pct"] is None
