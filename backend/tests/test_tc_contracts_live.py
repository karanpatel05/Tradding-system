import asyncio


def _run():
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import OptionPositionRecord, StrategyState
        from app.strategies.triple_calendar import live_source, strategy_class
        from sqlalchemy import select

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02
        # user set 3 contracts on the strategy
        async with session_scope() as s:
            st = await s.get(StrategyState, "strategy_2")
            if st is None:
                st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True)
                s.add(st)
            st.contracts = 3

        async def a_spot(self, sym): return 445.0
        async def a_chain(self, u): return [float(r) for r in range(400, 491, 5)]
        async def a_mid(self, u, e, k, r): return 1.0 + 0.1 * max((e - __import__("datetime").date.today()).days, 0)
        async def a_vix(self): return 12.0
        live_source.LivePriceSource.aunderlying = a_spot
        live_source.LivePriceSource.achain = a_chain
        live_source.LivePriceSource.aoption_mid = a_mid
        live_source.LivePriceSource.avix = a_vix
        strategy_class.get_market_status = lambda: {"is_open": True}
        strategy_class.broker_ready = lambda: True

        strat = strategy_class.TripleCalendarStrategy()
        task = asyncio.create_task(strat.run_live())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        async with session_scope() as s:
            rec = (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == "strategy_2",
                OptionPositionRecord.status == "open"))).scalars().first()
        assert rec is not None and rec.contracts == 3, f"expected contracts=3, got {rec and rec.contracts}"
        print(f"PASS: entered position persisted contracts={rec.contracts}")

    asyncio.run(_t())
    print("\nALL TC-CONTRACTS-LIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
