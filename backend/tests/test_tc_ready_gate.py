"""The Triple Calendar live loop only acts (manage_step/order actions) when the broker is
ready AND the regular market session is open — so it pauses during disconnect/recovery/
SAFE_MODE and outside RTH (no entering/managing on stale pre/post-market quotes). Facade
data still flows and valuations persist regardless (monitor-only, covered separately by
test_tc_monitor_only) — so the gate must be asserted on manage_step calls, not on whether
the price source was hit at all.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_tg.db python -m tests.test_tc_ready_gate
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db
        from app.strategies.triple_calendar import live_source, strategy_class

        await init_db()
        get_settings().tc_manage_interval_seconds = 0.02

        calls: list = []
        async def spy_manage_step(day, source, params, executor, position, events=None, vix=None,
                                  contracts=1):
            calls.append(1)
            return {"action": "none", "position": None, "info": {}}
        strategy_class.manage_step = spy_manage_step  # type: ignore[assignment]

        async def a_spot(self, sym):
            return 445.0
        async def a_chain(self, u):
            return [float(r) for r in range(400, 491, 5)]
        async def a_mid(self, *a):
            return 1.0
        async def a_vix(self):  # low VIX so the flat-entry gate doesn't block the tick
            return 12.0
        live_source.LivePriceSource.aunderlying = a_spot   # type: ignore[assignment]
        live_source.LivePriceSource.achain = a_chain       # type: ignore[assignment]
        live_source.LivePriceSource.aoption_mid = a_mid     # type: ignore[assignment]
        live_source.LivePriceSource.avix = a_vix            # type: ignore[assignment]

        strat = strategy_class.TripleCalendarStrategy()

        async def _spin(ready, market_open):
            strategy_class.get_market_status = lambda: {"is_open": market_open}  # type: ignore[assignment]
            strategy_class.broker_ready = lambda: ready  # type: ignore[assignment]
            calls.clear()
            task = asyncio.create_task(strat.run_live())
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return len(calls)

        assert await _spin(False, True) == 0, "manage_step ran while not READY"
        assert await _spin(True, False) == 0, "manage_step ran while market closed"
        acted = await _spin(True, True)
        assert acted > 0, "manage_step did not run while READY + market open"
        print(f"PASS: TC loop gated on READY + market-open (ready+open manage_step calls={acted})")
    asyncio.run(_t())
    print("\nALL TC-READY-GATE TESTS PASSED")


if __name__ == "__main__":
    _run()
