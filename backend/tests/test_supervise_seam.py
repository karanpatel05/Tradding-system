"""A connected socket stuck in CONNECTED is driven to READY by one supervisor tick.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_seam.db python -m tests.test_supervise_seam
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()

        class CleanIB:
            def isConnected(self): return True
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        gw._ib = CleanIB()
        gw.set_state(ConnState.CONNECTED)   # reconnected, but recovery never ran
        await gw._supervise_tick()
        assert gw.state is ConnState.READY, gw.state
        print("PASS: supervisor drives CONNECTED -> READY")
    asyncio.run(_t())
    print("\nALL SUPERVISE-SEAM TESTS PASSED")


if __name__ == "__main__":
    _run()
