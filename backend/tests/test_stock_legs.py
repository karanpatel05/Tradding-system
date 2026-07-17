"""Stock legs (right='S'): linear payoff, spot-priced quotes (never theo), payoff
endpoint tolerates empty expiry, covered-call structure values correctly."""
from datetime import date, timedelta

import pytest

from app.options.combo import combo_value, payoff_at
from app.routers import builder

EXP = (date.today() + timedelta(days=30)).isoformat()


def test_payoff_at_stock_linear():
    # 100 shares = 1.0 per-1x unit: payoff_curve's x100 then yields qty x S dollars.
    legs = [{"action": "BUY", "right": "S", "strike": 0.0, "expiry": "", "qty": 100}]
    assert payoff_at(legs, 50.0, date.today(), 0.2, 0.04) == pytest.approx(50.0)
    assert payoff_at(legs, 60.0, date.today(), 0.2, 0.04) == pytest.approx(60.0)


def test_covered_call_value():
    legs = [{"action": "BUY", "right": "S", "strike": 0.0, "expiry": "", "qty": 100},
            {"action": "SELL", "right": "C", "strike": 105.0, "expiry": EXP, "qty": 1}]
    # past expiry at S=120 (per-1x): stock 120 - short-call intrinsic 15 = 105 — the
    # classic covered-call value S - max(S-K, 0).
    v = payoff_at(legs, 120.0, date.fromisoformat(EXP) + timedelta(days=1), 0.2, 0.0)
    assert v == pytest.approx(105.0, abs=0.01)


def test_combo_value_stock_uses_mid():
    legs = [{"action": "BUY", "right": "S", "strike": 0.0, "expiry": "", "qty": 100}]
    assert combo_value(legs, [101.5]) == pytest.approx(101.5)   # per-1x (100 sh = 1.0)


@pytest.mark.asyncio
async def test_quote_stock_leg_spot_never_theo(monkeypatch):
    async def fake_mids(underlying, legs):
        return [None] * len(legs)      # options side missing
    monkeypatch.setattr(builder.chain_mod, "get_leg_mids", fake_mids)
    body = builder.QuoteBody(underlying="QQQ", spot=100.0, iv=0.25, legs=[
        builder.Leg(action="BUY", right="S", strike=0.0, expiry="", qty=100)])
    out = await builder.quote(body)
    assert out["legs"][0]["mid"] == 100.0 and out["legs"][0]["theo"] is False
    assert out["theoretical"] is False


@pytest.mark.asyncio
async def test_payoff_all_stock_defaults_today(monkeypatch):
    body = builder.PayoffBody(net_debit=100.0, iv=0.25, spot=100.0, legs=[
        builder.Leg(action="BUY", right="S", strike=0.0, expiry="", qty=100)])
    out = await builder.payoff(body)
    assert out["eval_date"] == date.today().isoformat()
    assert len(out["curve"]) >= 2


@pytest.mark.asyncio
async def test_get_trade_route_all_stock_defaults_today():
    """A saved all-stock trade (empty expiry) must not crash the reopen/monitor route's
    eval_date derivation — mirrors the /payoff endpoint's guard."""
    from app.db.base import init_db

    await init_db()
    saved = await builder.save_trade(builder.SaveBody(
        underlying="QQQ", net_debit=10000.0, iv=0.25,
        legs=[builder.Leg(action="BUY", right="S", strike=0.0, expiry="", qty=100)]))
    out = await builder.get_trade_route(saved["id"])
    assert out["eval_date"] == date.today().isoformat()
    assert len(out["curve"]) >= 2
