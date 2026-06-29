"""Heartbeat miss flips state to DISCONNECTED.
   Run: python -m tests.test_heartbeat
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.ibkr.gateway import ConnState, Gateway

    class HangingIB:
        def isConnected(self): return True
        async def reqCurrentTimeAsync(self):
            await asyncio.sleep(10)   # never returns within timeout

    async def _t():
        gw = Gateway()
        gw._ib = HangingIB()
        gw.set_state(ConnState.READY)
        ok = await gw.heartbeat_once(timeout=0.05)
        assert ok is False
        assert gw.state is ConnState.DISCONNECTED, gw.state
    asyncio.run(_t())
    print("PASS: heartbeat timeout -> DISCONNECTED")

    print("\nALL HEARTBEAT TESTS PASSED")


if __name__ == "__main__":
    _run()
