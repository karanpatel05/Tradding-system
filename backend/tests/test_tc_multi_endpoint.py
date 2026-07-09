# backend/tests/test_tc_multi_endpoint.py
from datetime import date, timedelta

import pytest

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.routers import strategies


@pytest.mark.asyncio
async def test_options_position_lists_all_tickers():
    await init_db()
    today = date.today()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
            s.add(st)
        st.tc_tickers = [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 1}]
        # QQQ has an open position; SPY is flat
        s.add(OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="open", paper=True,
            net_debit=4.0, contracts=2, last_value=4.2, last_pnl_pct=0.05, last_spot=501.2,
            calendars=[{"strike": 500.0, "sell": (today + timedelta(days=21)).isoformat(),
                        "buy": (today + timedelta(days=28)).isoformat()}], order_ids=[]))
    out = await strategies.options_position("strategy_2")
    by = {t["ticker"]: t for t in out["tickers"]}
    assert set(by) == {"QQQ", "SPY"}
    assert by["QQQ"]["has_position"] is True
    assert by["QQQ"]["unrealized_pnl"] == pytest.approx(40.0)   # (4.2-4.0)*100*2
    assert by["SPY"]["has_position"] is False and by["SPY"]["contracts"] == 1
