# backend/tests/test_payoff_endpoint.py
"""GET /api/strategies/{key}/options-position/payoff. Run:
DATABASE_URL=sqlite+aiosqlite:///./_t_payoff_ep.db python -m pytest tests/test_payoff_endpoint.py"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord
from app.main import app

CALS = [
    {"strike": 470.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 480.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 490.0, "sell": "2026-08-21", "buy": "2026-09-18"},
]


@pytest.mark.asyncio
async def test_payoff_endpoint_open_position():
    await init_db()
    async with session_scope() as s:
        s.add(OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="open", paper=True,
            net_debit=4.0, last_value=4.6, last_spot=480.0, calendars=CALS,
            entry_time=datetime.now(timezone.utc)))
    c = TestClient(app)
    r = c.get("/api/strategies/strategy_2/options-position/payoff?ticker=QQQ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_position"] is True
    assert body["ticker"] == "QQQ"
    assert len(body["curve"]) == 61
    assert set(body["greeks"]) == {"delta", "gamma", "theta", "vega"}


@pytest.mark.asyncio
async def test_payoff_endpoint_flat():
    await init_db()
    c = TestClient(app)
    r = c.get("/api/strategies/strategy_2/options-position/payoff?ticker=SPY")
    assert r.status_code == 200
    assert r.json() == {"has_position": False, "ticker": "SPY"}
