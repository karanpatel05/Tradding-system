"""Clean recovery schedules a backfill task (non-blocking).
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rb.db python -m tests.test_recovery_backfill
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine import reconciliation as recon_mod
        from app.engine import recovery

        await init_db()
        scheduled = {"n": 0}
        recovery._schedule_backfill = lambda: scheduled.__setitem__("n", scheduled["n"] + 1)  # type: ignore[assignment]

        async def no_positions():
            return []

        async def no_orders():
            return []

        recon_mod._positions = no_positions
        recon_mod._live_orders = no_orders

        ok = await recovery.run_recovery()
        assert ok is True
        assert scheduled["n"] == 1, scheduled
        print("PASS: clean recovery scheduled backfill")
    asyncio.run(_t())
    print("\nALL RECOVERY-BACKFILL TESTS PASSED")


if __name__ == "__main__":
    _run()
