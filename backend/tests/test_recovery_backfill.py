"""Clean recovery schedules a backfill task (non-blocking).
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rb.db python -m tests.test_recovery_backfill
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine import recovery
        from app.ibkr.gateway import ConnState, Gateway

        await init_db()
        from app.config import get_settings
        get_settings().ibkr_account_settle_seconds = 0  # no settle wait in tests
        scheduled = {"n": 0}
        recovery._schedule_backfill = lambda: scheduled.__setitem__("n", scheduled["n"] + 1)  # type: ignore[assignment]

        class CleanIB:
            async def reqPositionsAsync(self): return []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        ok = await recovery.run_recovery(CleanIB(), gw)
        assert ok is True and gw.state is ConnState.READY
        assert scheduled["n"] == 1, scheduled
        print("PASS: clean recovery scheduled backfill")
    asyncio.run(_t())
    print("\nALL RECOVERY-BACKFILL TESTS PASSED")


if __name__ == "__main__":
    _run()
