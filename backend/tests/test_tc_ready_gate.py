"""The Triple Calendar live loop only acts when the connection is READY AND the regular
market session is open — so it pauses during disconnect/recovery/SAFE_MODE and outside RTH
(no entering/managing on stale pre/post-market quotes).
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_tg.db python -m tests.test_tc_ready_gate
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db
        from app.ibkr.gateway import ConnState, get_gateway
        from app.strategies.triple_calendar import live_source, strategy_class

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02

        calls = {"n": 0}
        async def fake_aunderlying(self, sym):  # counts ticks that reached the price source
            calls["n"] += 1
            return None
        live_source.LivePriceSource.aunderlying = fake_aunderlying  # type: ignore[assignment]

        strat = strategy_class.TripleCalendarStrategy()

        async def _spin(state, market_open):
            strategy_class.get_market_status = lambda: {"is_open": market_open}  # type: ignore[assignment]
            get_gateway().set_state(state)
            calls["n"] = 0
            task = asyncio.create_task(strat.run_live())
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return calls["n"]

        assert await _spin(ConnState.DISCONNECTED, True) == 0, "acted while not READY"
        assert await _spin(ConnState.READY, False) == 0, "acted while market closed"
        acted = await _spin(ConnState.READY, True)
        assert acted > 0, "did not act while READY + market open"
        print(f"PASS: TC loop gated on READY + market-open (ready+open ticks={acted})")
    asyncio.run(_t())
    print("\nALL TC-READY-GATE TESTS PASSED")


if __name__ == "__main__":
    _run()
