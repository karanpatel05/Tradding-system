"""Safe mode persists and blocks order submission.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_safe.db python -m tests.test_safe_mode
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.safe_mode import clear_safe_mode, is_safe_mode, trip_safe_mode
        from app.engine.execution import submit_equity_order

        await init_db()
        assert await is_safe_mode() is False
        await trip_safe_mode("orphan position AAPL")
        assert await is_safe_mode() is True

        res = await submit_equity_order("AAPL", "enter_long", 10, reason="entry")
        assert res["transmitted"] is False and res.get("safe_mode") is True, res
        print("PASS: safe mode blocks submission")

        await clear_safe_mode()
        assert await is_safe_mode() is False
        print("PASS: safe mode clears")

    asyncio.run(_t())
    print("\nALL SAFE-MODE TESTS PASSED")


if __name__ == "__main__":
    _run()
