"""Recovery: clean broker state -> READY; orphan position -> SAFE_MODE.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rec.db python -m tests.test_recovery_sequence
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.recovery import run_recovery
        from app.engine.safe_mode import is_safe_mode
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()

        class C:
            def __init__(self, sym): self.symbol = sym; self.secType = "STK"
        class Pos:
            def __init__(self, sym, q): self.contract = C(sym); self.position = q; self.avgCost = 100.0

        class CleanIB:
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []
        class OrphanIB(CleanIB):
            async def reqPositionsAsync(self): return [Pos("AAPL", 10)]

        gw = Gateway()
        ok = await run_recovery(CleanIB(), gw)
        assert ok is True and gw.state is ConnState.READY, gw.state
        assert await is_safe_mode() is False
        print("PASS: clean recovery -> READY")

        gw2 = Gateway()
        ok2 = await run_recovery(OrphanIB(), gw2)
        assert ok2 is False and gw2.state is ConnState.SAFE_MODE, gw2.state
        assert await is_safe_mode() is True
        print("PASS: orphan position -> SAFE_MODE")

    asyncio.run(_t())
    print("\nALL RECOVERY TESTS PASSED")


if __name__ == "__main__":
    _run()
