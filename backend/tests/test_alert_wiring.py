"""Recovery SAFE_MODE sends a safe_mode alert; disconnect fires one.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_aw.db python -m tests.test_alert_wiring
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine import reconciliation as recon_mod
        from app.engine import recovery
        from app.ibkr.gateway import Gateway
        from app.services import alerts

        await init_db()
        seen = []
        async def cap(kind, msg, data): seen.append(kind)
        alerts.register_channel(cap)

        # An open local trade with no broker position → reconcile mismatch → SAFE_MODE.
        async with session_scope() as s:
            s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                              quantity=10, status="open"))

        async def no_positions():
            return []

        async def no_orders():
            return []

        recon_mod._positions = no_positions
        recon_mod._live_orders = no_orders

        ok = await recovery.run_recovery()
        assert ok is False
        assert "safe_mode" in seen, seen

        # Disconnect alert is Gateway's own event wiring — unrelated to recovery.
        gw = Gateway()
        gw._on_disconnect()
        await asyncio.sleep(0.2)   # let fire_alert's scheduled task run to completion
        assert "disconnect" in seen, seen
        print("PASS: safe_mode + disconnect alerts fired")
    asyncio.run(_t())
    print("\nALL ALERT-WIRING TESTS PASSED")


if __name__ == "__main__":
    _run()
