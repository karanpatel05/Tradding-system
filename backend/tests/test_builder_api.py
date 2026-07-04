from datetime import date, timedelta

import pytest

from app.db.base import init_db
from app.routers import builder

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0,
         "expiry": (date.today() + timedelta(days=28)).isoformat(), "qty": 1},
        {"action": "SELL", "right": "C", "strike": 105.0,
         "expiry": (date.today() + timedelta(days=28)).isoformat(), "qty": 1}]


@pytest.mark.asyncio
async def test_payoff_and_save_and_list():
    await init_db()
    payoff = await builder.payoff(builder.PayoffBody(
        legs=LEGS, net_debit=2.0, iv=0.25, points=41))
    assert len(payoff["curve"]) == 41 and payoff["max_profit"] > 0

    saved = await builder.save_trade(builder.SaveBody(
        underlying="QQQ", legs=LEGS, net_debit=2.0, iv=0.25,
        target_pct=0.10, stop_pct=0.25))
    assert saved["status"] == "open" and saved["id"]

    listing = await builder.list_trades_route()
    assert any(t["id"] == saved["id"] for t in listing["trades"])

    closed = await builder.close_trade_route(saved["id"])
    assert closed["status"] == "closed" and closed["exit_reason"] == "manual"


@pytest.mark.asyncio
async def test_empty_legs_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        builder.PayoffBody(legs=[], net_debit=2.0, iv=0.25)
    with pytest.raises(ValidationError):
        builder.SaveBody(underlying="QQQ", legs=[], net_debit=2.0, iv=0.25)


@pytest.mark.asyncio
async def test_invalid_leg_action_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        builder.Leg(action="buy", right="C", strike=100.0, expiry="2026-07-31", qty=1)
