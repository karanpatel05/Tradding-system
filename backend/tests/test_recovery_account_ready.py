"""Recovery waits for account data to re-sync before reconciling, so a just-restarted
Gateway (positions briefly empty) does NOT false-trip SAFE_MODE.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_ar.db python -m tests.test_recovery_account_ready
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine import recovery
        from app.ibkr.gateway import ConnState, Gateway

        get_settings().ibkr_account_settle_seconds = 0   # no real settle wait in the test
        await init_db()
        async with session_scope() as s:
            s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                              quantity=10, status="open"))

        class C:
            def __init__(self, sym): self.symbol = sym; self.secType = "STK"
        class Pos:
            def __init__(self, sym, q): self.contract = C(sym); self.position = q; self.avgCost = 100.0

        class RestartingIB:
            """Accounts + positions are empty until 'synced' (2nd managedAccounts poll)."""
            def __init__(self): self.polls = 0
            def managedAccounts(self):
                self.polls += 1
                return ["DU1"] if self.polls >= 2 else []
            async def reqPositionsAsync(self):
                return [Pos("AAPL", 10)] if self.polls >= 2 else []
            async def reqExecutionsAsync(self, flt): return []
            async def reqAllOpenOrdersAsync(self): return []

        gw = Gateway()
        ok = await recovery.run_recovery(RestartingIB(), gw)
        assert ok is True and gw.state is ConnState.READY, gw.state
        print("PASS: recovery waited for account sync, reconciled to READY")
    asyncio.run(_t())
    print("\nALL RECOVERY-ACCOUNT-READY TESTS PASSED")


if __name__ == "__main__":
    _run()
