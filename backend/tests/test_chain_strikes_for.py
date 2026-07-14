"""facade.chain_strikes_for: per-expiry strikes from the TT chain's by_key map;
IBKR fallback returns the union (best it offers)."""
import pytest

from app.marketdata import facade


@pytest.mark.asyncio
async def test_filters_by_expiry(monkeypatch):
    async def fake_chain(u):
        return {"expiries": ["2026-08-07", "2026-08-21"],
                "strikes": [95.0, 100.0, 105.0],
                "by_key": {("2026-08-07", 100.0, "C"): "a", ("2026-08-07", 100.0, "P"): "b",
                           ("2026-08-07", 105.0, "C"): "c", ("2026-08-21", 95.0, "C"): "d"}}
    monkeypatch.setattr(facade, "_tt_chain", fake_chain)
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    facade._last_source.clear(); facade._last_source_ts.clear()
    assert await facade.chain_strikes_for("QQQ", "2026-08-07") == [100.0, 105.0]
    assert await facade.chain_strikes_for("QQQ", "2026-08-21") == [95.0]
    assert await facade.chain_strikes_for("QQQ", "2027-01-01") == []  # unknown expiry -> no TT hit, no IB
