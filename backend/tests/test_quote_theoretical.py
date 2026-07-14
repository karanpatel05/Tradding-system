"""/api/builder/quote: legs with no live mid fall back to Black-Scholes theoretical
values when a spot is supplied — flagged per-leg and overall so the UI can gate Save."""
from datetime import date, timedelta

import pytest

from app.options.blackscholes import bs_price
from app.routers import builder


@pytest.mark.asyncio
async def test_theoretical_fill_when_no_live_quote(monkeypatch):
    async def no_mids(underlying, legs):
        return [None]
    monkeypatch.setattr(builder.chain_mod, "get_leg_mids", no_mids)
    expiry = (date.today() + timedelta(days=30)).isoformat()
    body = builder.QuoteBody(underlying="QQQ", spot=100.0, iv=0.25, r=0.04, legs=[
        builder.Leg(action="BUY", right="C", strike=100.0, expiry=expiry, qty=1)])
    out = await builder.quote(body)
    expected = round(bs_price(100.0, 100.0, 30 / 365.0, 0.25, 0.04, "C"), 4)
    assert out["legs"][0]["mid"] == expected and out["legs"][0]["theo"] is True
    assert out["theoretical"] is True
    assert out["net_debit"] == expected      # BUY 1 -> debit = the theo mid
    assert out["missing"] == []              # filled, not missing


@pytest.mark.asyncio
async def test_live_mid_wins_and_no_spot_stays_missing(monkeypatch):
    async def one_mid(underlying, legs):
        return [2.5, None]
    monkeypatch.setattr(builder.chain_mod, "get_leg_mids", one_mid)
    expiry = (date.today() + timedelta(days=30)).isoformat()
    legs = [builder.Leg(action="BUY", right="C", strike=100.0, expiry=expiry, qty=1),
            builder.Leg(action="SELL", right="C", strike=110.0, expiry=expiry, qty=1)]
    # With spot: live leg stays live, unquoted leg is theo-filled.
    out = await builder.quote(builder.QuoteBody(underlying="QQQ", spot=100.0, legs=legs))
    assert out["legs"][0]["mid"] == 2.5 and out["legs"][0]["theo"] is False
    assert out["legs"][1]["theo"] is True and out["theoretical"] is True
    # Without spot: unquoted legs remain missing (unchanged legacy behavior).
    out = await builder.quote(builder.QuoteBody(underlying="QQQ", legs=legs))
    assert out["missing"] == [1] and out["net_debit"] is None and out["theoretical"] is False
