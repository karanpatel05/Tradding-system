# backend/tests/test_consumers_facade.py
"""Consumers read through the facade: overview builds rows without IBKR auth; the
builder chain composes facade expiries+strikes and falls back to IBKR shape."""
import pytest

from app.marketdata import facade


@pytest.mark.asyncio
async def test_overview_without_gateway(monkeypatch):
    from app.services import market as m
    async def fake_quotes(symbols, timeout=2.5):
        return {s: {"bid": 1.0, "ask": 1.2, "last": 1.1, "close": None, "price": 1.1}
                for s in symbols}
    monkeypatch.setattr(m, "facade_stock_quotes", fake_quotes)
    async def fake_weekly(symbols):
        return {}
    monkeypatch.setattr(m, "_weekly_changes", fake_weekly)
    out = await m.get_market_overview()
    assert out["indices"] and out["indices"][0]["last"] == 1.1


@pytest.mark.asyncio
async def test_builder_chain_prefers_facade(monkeypatch):
    from app.options import chain as ch
    async def fake_exp(u): return ["2026-07-30"]
    async def fake_strikes(u): return [495.0, 500.0]
    monkeypatch.setattr(ch.facade, "chain_expiries", fake_exp)
    monkeypatch.setattr(ch.facade, "chain_strikes", fake_strikes)
    out = await ch.get_option_chain("QQQ")
    assert out == {"underlying": "QQQ", "expirations": ["2026-07-30"],
                   "strikes": [495.0, 500.0]}
