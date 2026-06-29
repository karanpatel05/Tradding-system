"""connect_and_recover connects then runs recovery to READY on clean state.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_sr.db python -m tests.test_startup_recovery
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()

        class FakeEvent:
            def __iadd__(self, h):
                return self

        class CleanIB:
            def __init__(self):
                self.disconnectedEvent = FakeEvent()
                self.errorEvent = FakeEvent()
                self._connected = False

            async def connectAsync(self, *a, **k):
                self._connected = True
                return self

            def isConnected(self):
                return self._connected

            def reqMarketDataType(self, *_):
                pass

            async def reqPositionsAsync(self):
                return []

            async def reqExecutionsAsync(self, flt):
                return []

            async def reqAllOpenOrdersAsync(self):
                return []

        gw = Gateway(ib_factory=CleanIB)
        ok = await gw.connect_and_recover()
        assert ok is True and gw.state is ConnState.READY, gw.state
        print("PASS: startup connect_and_recover -> READY")

    asyncio.run(_t())
    print("\nALL STARTUP-RECOVERY TESTS PASSED")


if __name__ == "__main__":
    _run()
