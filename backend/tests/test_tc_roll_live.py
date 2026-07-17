"""The live loop persists a roll (closes the old record, opens a new one) and fast-polls VIX
while flat + waiting for VIX <= 16.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rl.db python -m tests.test_tc_roll_live
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import OptionPositionRecord
        from app.strategies.triple_calendar import live_source, strategy_class
        from app.strategies.triple_calendar.legs import Calendar, Position
        from app.strategies.triple_calendar.store import save_position
        from sqlalchemy import select

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02
        get_settings().vix_wait_poll_seconds = 0.02

        today = date.today()
        sell, buy = today + timedelta(days=21), today + timedelta(days=28)
        # Seed an open position already at a -25% loss so the first managed tick rolls it.
        pos = Position("QQQ", today, [Calendar(725.0, sell, buy)], net_debit=10.0)
        old_id = await save_position("strategy_2", pos, order_ids=[], paper=True)

        strikes = [float(r) for r in range(400, 491, 5)]

        async def a_spot(self, sym):
            return 445.0
        async def a_chain(self, u):
            return strikes
        async def a_mid(self, u, expiry, strike, right):
            # seeded 725 position: buy leg -> 7.5, sell leg -> 0, so value 7.5 on debit 10 =
            # -25% -> rolls. fresh entry strikes (~445) use the DTE curve (positive debit).
            if strike == 725.0:
                return 7.5 if expiry == buy else 0.0
            return 1.0 + 0.1 * max((expiry - today).days, 0)
        async def a_vix(self):
            return 12.0
        live_source.LivePriceSource.aunderlying = a_spot   # type: ignore[assignment]
        live_source.LivePriceSource.achain = a_chain       # type: ignore[assignment]
        live_source.LivePriceSource.aoption_mid = a_mid     # type: ignore[assignment]
        live_source.LivePriceSource.avix = a_vix            # type: ignore[assignment]
        strategy_class.get_market_status = lambda: {"is_open": True}  # type: ignore[assignment]
        strategy_class.broker_ready = lambda: True  # type: ignore[assignment]

        strat = strategy_class.TripleCalendarStrategy()
        task = asyncio.create_task(strat.run_live())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        async with session_scope() as s:
            old = await s.get(OptionPositionRecord, old_id)
            open_rows = (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == "strategy_2",
                OptionPositionRecord.status == "open"))).scalars().all()
        assert old.status == "closed", "the rolled position should be closed"
        assert old.pnl is not None and old.pnl < 0, f"closed at a loss, got {old.pnl}"
        assert len(open_rows) == 1 and open_rows[0].id != old_id, "a fresh position should be open"
        print(f"PASS: roll persisted — old #{old_id} closed pnl={old.pnl:.2f}, new #{open_rows[0].id} open")

    asyncio.run(_t())
    print("\nALL TC-ROLL-LIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
