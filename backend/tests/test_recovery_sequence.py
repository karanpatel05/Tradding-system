"""Recovery: clean broker state -> READY; orphan position -> SAFE_MODE.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_rec.db python -m tests.test_recovery_sequence
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.broker.account import NormalizedPosition
        from app.db.base import init_db
        from app.engine import reconciliation as recon_mod
        from app.engine.recovery import run_recovery
        from app.engine.safe_mode import clear_safe_mode, is_safe_mode

        await init_db()

        def pos(sym, qty):
            return NormalizedPosition(symbol=sym, quantity=qty, quantity_direction="Long",
                                      instrument_type="Equity", avg_price=100.0, account="X")

        async def no_positions():
            return []

        async def orphan_positions():
            return [pos("AAPL", 10)]

        async def no_orders():
            return []

        recon_mod._positions = no_positions
        recon_mod._live_orders = no_orders
        await clear_safe_mode()
        ok = await run_recovery()
        assert ok is True, ok
        assert await is_safe_mode() is False
        print("PASS: clean recovery -> READY")

        recon_mod._positions = orphan_positions
        ok2 = await run_recovery()
        assert ok2 is False, ok2
        assert await is_safe_mode() is True
        print("PASS: orphan position -> SAFE_MODE")
        await clear_safe_mode()

    asyncio.run(_t())
    print("\nALL RECOVERY TESTS PASSED")


if __name__ == "__main__":
    _run()
