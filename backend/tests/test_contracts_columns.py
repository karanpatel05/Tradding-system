import pytest

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.strategies.triple_calendar.legs import Calendar, Position
from datetime import date


def test_position_dataclass_has_contracts():
    p = Position("QQQ", date(2026, 1, 1))
    assert p.contracts == 1
    p2 = Position("QQQ", date(2026, 1, 1), contracts=3)
    assert p2.contracts == 3


@pytest.mark.asyncio
async def test_contracts_columns_round_trip():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
            s.add(st)
        st.contracts = 2
        rec = OptionPositionRecord(strategy_key="strategy_2", underlying="QQQ",
                                   net_debit=4.0, contracts=5, calendars=[], order_ids=[])
        s.add(rec)
        await s.flush()
        rid = rec.id
    async with session_scope() as s:
        assert (await s.get(StrategyState, "strategy_2")).contracts == 2
        assert (await s.get(OptionPositionRecord, rid)).contracts == 5
