"""implied_vol inverts bs_price by bisection (round-trip to 1e-3); /iv-smile builds
{strike, iv, side} rows from live mids (calls >= spot, puts < spot), skipping
strikes with no mid or no root."""
from datetime import date, timedelta

import pytest

from app.options.blackscholes import bs_price, implied_vol
from app.routers import builder


def test_round_trip():
    S, K, T, r = 100.0, 105.0, 0.25, 0.04
    for true_iv in (0.12, 0.25, 0.60, 1.50):
        price = bs_price(S, K, T, true_iv, r, "C")
        assert implied_vol(price, S, K, T, r, "C") == pytest.approx(true_iv, abs=1e-3)


def test_no_root_returns_none():
    # ITM call priced BELOW its intrinsic floor (~101 here): both endpoints of the
    # bisection bracket sit above the target price (same sign) -> no root, by construction.
    assert implied_vol(50.0, 200.0, 100.0, 0.25, 0.04, "C") is None
    assert implied_vol(1000.0, 100.0, 100.0, 0.25, 0.04, "C") is None      # above any vol


@pytest.mark.asyncio
async def test_smile_endpoint(monkeypatch):
    exp = (date.today() + timedelta(days=30)).isoformat()
    T = 30 / 365.0
    async def fake_chain_quotes(underlying, expiry, count):
        rows = []
        for k, iv in ((95.0, 0.30), (100.0, 0.25), (105.0, 0.28)):
            rows.append({"strike": k,
                         "call_mid": round(bs_price(100.0, k, T, iv, 0.04, "C"), 4),
                         "put_mid": round(bs_price(100.0, k, T, iv, 0.04, "P"), 4)})
        rows.append({"strike": 110.0, "call_mid": None, "put_mid": None})   # skipped
        return {"underlying": underlying, "expiry": expiry, "spot": 100.0,
                "atm": 100.0, "count": count, "rows": rows}
    monkeypatch.setattr(builder.chain_mod, "get_chain_quotes", fake_chain_quotes)
    out = await builder.iv_smile("QQQ", exp, 10)
    got = {(p["strike"], p["side"]): p["iv"] for p in out["points"]}
    assert got[(95.0, "P")] == pytest.approx(0.30, abs=2e-3)    # below spot -> put side
    assert got[(100.0, "C")] == pytest.approx(0.25, abs=2e-3)   # at/above spot -> call side
    assert got[(105.0, "C")] == pytest.approx(0.28, abs=2e-3)
    assert (110.0, "C") not in got and (110.0, "P") not in got
