import pytest

from app.options import chain


def _mk(monkeypatch, *, spot, strikes):
    async def fake_quote(u):
        return {"price": spot}

    async def fake_chain(u):
        return {"underlying": "QQQ", "expirations": [], "strikes": [float(s) for s in strikes]}

    async def fake_mid(self, u, expiry, strike, right):
        if strike == 500.0 and right == "P":
            return None  # a null inside the window -> must survive to the row
        return round(strike / 100 + (0.1 if right == "C" else 0.2), 2)

    monkeypatch.setattr(chain.facade, "stock_quote", fake_quote)
    monkeypatch.setattr(chain, "get_option_chain", fake_chain)
    monkeypatch.setattr(chain.LivePriceSource, "aoption_mid", fake_mid)


@pytest.mark.asyncio
async def test_windows_around_atm_with_null(monkeypatch):
    _mk(monkeypatch, spot=501.2, strikes=range(460, 541, 5))
    out = await chain.get_chain_quotes("QQQ", "2026-07-17", count=3)
    assert out["spot"] == 501.2 and out["atm"] == 500.0
    assert [r["strike"] for r in out["rows"]] == [485.0, 490.0, 495.0, 500.0, 505.0, 510.0, 515.0]
    atm_row = next(r for r in out["rows"] if r["strike"] == 500.0)
    assert atm_row["call_mid"] is not None and atm_row["put_mid"] is None
    assert all("call_mid" in r and "put_mid" in r for r in out["rows"])


@pytest.mark.asyncio
async def test_unavailable_spot_returns_empty(monkeypatch):
    _mk(monkeypatch, spot=-1.0, strikes=[490, 495, 500])  # IBKR sentinel
    out = await chain.get_chain_quotes("QQQ", "2026-07-17", count=3)
    assert out["atm"] is None and out["rows"] == []


@pytest.mark.asyncio
async def test_window_clamps_at_chain_edge(monkeypatch):
    _mk(monkeypatch, spot=501.2, strikes=[490, 495, 500])  # ATM at the top edge
    out = await chain.get_chain_quotes("QQQ", "2026-07-17", count=3)
    assert [r["strike"] for r in out["rows"]] == [490.0, 495.0, 500.0]  # clamped, no error
