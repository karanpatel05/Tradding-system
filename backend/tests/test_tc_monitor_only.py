# backend/tests/test_tc_monitor_only.py
"""Gateway NOT READY + market open -> the loop still values the open position via the
(faked) facade and persists update_value, but manage_step/executor are never invoked.
Run: DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db python -m tests.test_tc_monitor_only"""
import asyncio
from datetime import date, timedelta

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.strategies.triple_calendar import strategy_class as sc
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import load_open_id, save_position

KEY = "strategy_2"


class _Src:
    async def aunderlying(self, s): return 500.0
    async def avix(self, s="VIX"): return 14.0
    async def aoption_mid(self, u, e, k, r): return 2.10
    async def achain(self, u): return [495.0, 500.0, 505.0]


async def _main():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, KEY) or StrategyState(key=KEY, name="TC", ready=True)
        s.add(st); st.tc_tickers = []
        from sqlalchemy import update
        await s.execute(update(OptionPositionRecord)
                        .where(OptionPositionRecord.strategy_key == KEY,
                               OptionPositionRecord.status == "open")
                        .values(status="closed"))
    today = date.today()
    pos = Position("QQQ", today, [Calendar(500.0, today + timedelta(days=21),
                                           today + timedelta(days=28))], net_debit=4.0)
    rid = await save_position(KEY, pos, order_ids=[], paper=True)

    strat = sc.TripleCalendarStrategy()
    calls = {"manage": 0}

    async def boom(*a, **kw):
        calls["manage"] += 1
        raise AssertionError("manage_step must not run while NOT READY")

    sc_ns = vars(sc)
    orig = dict(manage_step=sc_ns["manage_step"], LivePriceSource=sc_ns["LivePriceSource"])
    sc_ns["manage_step"] = boom
    sc_ns["LivePriceSource"] = _Src

    sc_ns_broker_ready = sc_ns["broker_ready"]
    sc_ns["broker_ready"] = lambda: False  # NOT READY
    sc_ns["get_market_status"] = lambda: {"is_open": True}

    task = asyncio.create_task(strat.run_live())
    await asyncio.sleep(1.0)          # let at least one valuation tick run
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    sc_ns.update(orig); sc_ns["broker_ready"] = sc_ns_broker_ready

    assert calls["manage"] == 0
    async with session_scope() as s:
        rec = await s.get(OptionPositionRecord, rid)
        assert rec.last_value is not None, "valuation-only tick must persist update_value"
    assert await load_open_id(KEY, "QQQ") == rid   # still open, untouched
    print("ALL TC MONITOR-ONLY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
