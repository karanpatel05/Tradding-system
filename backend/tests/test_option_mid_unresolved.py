"""facade.option_mid must degrade to None (never raise) when the requested
(expiry, strike, right) can't be resolved against the Tastytrade chain, or when the
chain itself is unavailable — this is what protected the builder /quote endpoint from
a 500 on an unresolved contract, now enforced at the facade level (sole data source).
   Run: python -m pytest tests/test_option_mid_unresolved.py -q
"""
from __future__ import annotations

from datetime import date

import pytest

from app.marketdata import facade


@pytest.mark.asyncio
async def test_option_mid_none_when_strike_not_in_chain(monkeypatch):
    async def fake_chain(underlying):
        return {"expiries": ["2026-07-18"], "strikes": [500.0],
                "by_key": {("2026-07-18", 500.0, "C"): ".QQQ260718C500"}}
    monkeypatch.setattr(facade, "_tt_chain", fake_chain)
    out = await facade.option_mid("QQQ", date(2026, 7, 18), 999.0, "C")
    assert out is None, "unresolved strike/right must yield None, not raise"


@pytest.mark.asyncio
async def test_option_mid_none_when_chain_unavailable(monkeypatch):
    async def fake_chain(underlying):
        return None
    monkeypatch.setattr(facade, "_tt_chain", fake_chain)
    out = await facade.option_mid("QQQ", date(2026, 7, 18), 500.0, "C")
    assert out is None, "a down Tastytrade chain must yield None, not raise"
