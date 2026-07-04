"""The live loop persists the valuation it already computes each tick onto the open
OptionPositionRecord (last_value/last_pnl_pct/last_spot/valued_at) — with NO extra IBKR
calls — so the detail page can show live P&L. Also covers restart-while-holding: the loop
recovers the record id via load_open_id and keeps updating the SAME record.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_lp.db python -m tests.test_tc_live_persist
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import session_scope
        from app.db.base import init_db
        from app.db.models import OptionPositionRecord
        from app.ibkr.gateway import ConnState, get_gateway
        from app.strategies.triple_calendar import live_source, strategy_class
        from app.strategies.triple_calendar.legs import Calendar, Position
        from app.strategies.triple_calendar.store import save_position

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02

        today = date.today()
        sell, buy = today + timedelta(days=21), today + timedelta(days=28)
        spot = 500.0
        # net_debit == the value the fake mids imply → pnl_pct 0 → a plain "hold" tick.
        per_cal = (1.0 + 0.1 * 28) - (1.0 + 0.1 * 21)  # far − near = 0.7
        pos = Position("QQQ", today, [Calendar(495.0, sell, buy),
                                      Calendar(500.0, sell, buy),
                                      Calendar(505.0, sell, buy)], net_debit=round(3 * per_cal, 4))
        # Seed the open record BEFORE run_live starts → simulates a restart mid-hold. The
        # loop must recover the id (load_open_id), not leave record_id None.
        rec_id = await save_position("strategy_2", pos, order_ids=[], paper=True)

        async def fake_underlying(self, sym):
            return spot
        async def fake_chain(self, u):
            return [float(r) for r in range(400, 601, 5)]
        async def fake_mid(self, u, expiry, strike, right):
            return 1.0 + 0.1 * max((expiry - today).days, 0)
        live_source.LivePriceSource.aunderlying = fake_underlying   # type: ignore[assignment]
        live_source.LivePriceSource.achain = fake_chain             # type: ignore[assignment]
        live_source.LivePriceSource.aoption_mid = fake_mid          # type: ignore[assignment]
        strategy_class.get_market_status = lambda: {"is_open": True}  # type: ignore[assignment]
        get_gateway().set_state(ConnState.READY)

        strat = strategy_class.TripleCalendarStrategy()
        task = asyncio.create_task(strat.run_live())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        async with session_scope() as session:
            rec = await session.get(OptionPositionRecord, rec_id)
        assert rec is not None and rec.status == "open", "position should still be open (hold)"
        assert rec.last_value is not None, "loop did not persist last_value (record_id not recovered?)"
        assert rec.valued_at is not None, "loop did not stamp valued_at"
        assert rec.last_spot == spot, f"expected spot {spot}, got {rec.last_spot}"
        assert abs(rec.last_value - pos.net_debit) < 0.01, "value should match the debit at pnl 0"
        assert abs(rec.last_pnl_pct) < 0.01, "pnl_pct should be ~0 at entry-equivalent value"
        print(f"PASS: loop persisted value={rec.last_value:.3f} pnl%={rec.last_pnl_pct:.4f} "
              f"spot={rec.last_spot} (record id recovered after restart)")

    asyncio.run(_t())
    print("\nALL TC-LIVE-PERSIST TESTS PASSED")


if __name__ == "__main__":
    _run()
