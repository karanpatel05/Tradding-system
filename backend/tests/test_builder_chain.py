"""get_option_chain is a facade-only pass-through: expiries/strikes come from
facade.chain_expiries/chain_strikes, and the underlying is normalized to uppercase."""
import pytest

from app.options import chain


@pytest.mark.asyncio
async def test_get_option_chain_uppercases_underlying(monkeypatch):
    async def fake_exp(u):
        return ["2026-07-24", "2026-07-31", "2026-08-07"]

    async def fake_strikes(u):
        return [495.0, 500.0, 505.0]

    monkeypatch.setattr(chain.facade, "chain_expiries", fake_exp)
    monkeypatch.setattr(chain.facade, "chain_strikes", fake_strikes)
    out = await chain.get_option_chain("qqq")
    assert out["underlying"] == "QQQ"
    assert out["expirations"] == ["2026-07-24", "2026-07-31", "2026-08-07"]
    assert out["strikes"] == [495.0, 500.0, 505.0]


@pytest.mark.asyncio
async def test_get_option_chain_empty_when_tt_down(monkeypatch):
    async def empty(u):
        return []

    monkeypatch.setattr(chain.facade, "chain_expiries", empty)
    monkeypatch.setattr(chain.facade, "chain_strikes", empty)
    out = await chain.get_option_chain("qqq")
    assert out == {"underlying": "QQQ", "expirations": [], "strikes": []}
