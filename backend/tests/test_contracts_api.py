from datetime import date, timedelta

import pytest

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord
from app.routers import strategies


@pytest.mark.asyncio
async def test_set_contracts_clamps_and_persists():
    await init_db()
    out = await strategies.set_contracts("strategy_2", strategies.ContractsBody(contracts=0))
    assert out["contracts"] == 1        # clamped to >= 1
    out = await strategies.set_contracts("strategy_2", strategies.ContractsBody(contracts=3))
    assert out["contracts"] == 3
    detail = await strategies.detail("strategy_2")
    assert detail["contracts"] == 3


@pytest.mark.asyncio
async def test_options_position_scales_pnl_by_contracts():
    await init_db()
    today = date.today()
    # Isolation-safe: ensure our seeded record is the only OPEN strategy_2 position, even if a
    # prior test in a shared DB left one behind.
    from sqlalchemy import update
    async with session_scope() as s:
        await s.execute(update(OptionPositionRecord)
                        .where(OptionPositionRecord.strategy_key == "strategy_2",
                               OptionPositionRecord.status == "open")
                        .values(status="closed"))
    async with session_scope() as s:
        rec = OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="open", paper=True,
            net_debit=4.0, contracts=2, last_value=4.2, last_pnl_pct=0.05, last_spot=501.2,
            calendars=[{"strike": 500.0, "sell": (today + timedelta(days=21)).isoformat(),
                        "buy": (today + timedelta(days=28)).isoformat()}], order_ids=[])
        s.add(rec)
    out = await strategies.options_position("strategy_2")
    assert out["contracts"] == 2
    # (4.2 - 4.0) * 100 * 2 = 40.0
    assert out["unrealized_pnl"] == pytest.approx(40.0)
