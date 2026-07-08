import pytest

from app.db.base import init_db, session_scope
from app.db.models import StrategyState


@pytest.mark.asyncio
async def test_tc_tickers_round_trip():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
            s.add(st)
        assert st.tc_tickers == []  # default
        st.tc_tickers = [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 1}]
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        assert st.tc_tickers[0]["ticker"] == "QQQ" and st.tc_tickers[1]["contracts"] == 1
