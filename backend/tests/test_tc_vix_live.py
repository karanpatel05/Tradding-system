"""The live loop wires VIX into entries: it fetches VIX when flat, skips the entry attempt
entirely when VIX can't be read (fail-closed), and otherwise passes VIX into manage_step so
the pure gate can enforce the <=16 ceiling.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_vixl.db python -m tests.test_tc_vix_live
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

        calls: list = []

        async def spy_manage_step(day, source, params, executor, position, events=None, vix=None):
            calls.append(vix)
            return {"action": "none", "position": None, "info": {}}
        strategy_class.manage_step = spy_manage_step  # type: ignore[assignment]

        async def a_spot(self, sym):
            return 445.0
        async def a_chain(self, u):
            return [float(r) for r in range(400, 491, 5)]
        async def a_mid(self, *a):
            return 1.0
        live_source.LivePriceSource.aunderlying = a_spot   # type: ignore[assignment]
        live_source.LivePriceSource.achain = a_chain       # type: ignore[assignment]
        live_source.LivePriceSource.aoption_mid = a_mid     # type: ignore[assignment]
        strategy_class.get_market_status = lambda: {"is_open": True}  # type: ignore[assignment]
        get_gateway().set_state(ConnState.READY)

        strat = strategy_class.TripleCalendarStrategy()

        async def _spin():
            calls.clear()
            task = asyncio.create_task(strat.run_live())
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Fail-closed: VIX unavailable while flat -> no entry attempt (manage_step not called).
        async def vix_none(self):
            return None
        live_source.LivePriceSource.avix = vix_none   # type: ignore[assignment]
        await _spin()
        assert calls == [], f"expected no entry attempt when VIX unavailable, got {calls}"

        # VIX available -> manage_step is called with that VIX so the pure gate can decide.
        async def vix_15(self):
            return 15.0
        live_source.LivePriceSource.avix = vix_15     # type: ignore[assignment]
        await _spin()
        assert calls and all(v == 15.0 for v in calls), f"expected vix=15.0 passed through, got {calls}"

        print(f"PASS: fail-closed on missing VIX; VIX passed to entry when present (ticks={len(calls)})")

    asyncio.run(_t())
    print("\nALL TC-VIX-LIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
