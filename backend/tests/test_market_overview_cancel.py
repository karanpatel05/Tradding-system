"""A cancelled child of the overview gather (CancelledError is a BaseException, not an
Exception) must degrade to "no weekly change", never a 500."""
import asyncio

import pytest

from app.services import market


@pytest.mark.asyncio
async def test_overview_survives_cancelled_weekly(monkeypatch):
    async def quotes(symbols, timeout=2.5):
        return {s: {"bid": 1.0, "ask": 1.2, "last": 1.1, "close": None, "price": 1.1}
                for s in symbols}

    async def weekly(symbols):
        raise asyncio.CancelledError()

    monkeypatch.setattr(market, "facade_stock_quotes", quotes)
    monkeypatch.setattr(market, "_weekly_changes", weekly)
    out = await market.get_market_overview()
    assert out["data_available"] is True
    assert all(ix["week_change_pct"] is None for ix in out["indices"])


@pytest.mark.asyncio
async def test_overview_survives_cancelled_quotes(monkeypatch):
    async def quotes(symbols, timeout=2.5):
        raise asyncio.CancelledError()

    async def weekly(symbols):
        return {}

    monkeypatch.setattr(market, "facade_stock_quotes", quotes)
    monkeypatch.setattr(market, "_weekly_changes", weekly)
    out = await market.get_market_overview()
    assert out["data_available"] is False and out["note"]
