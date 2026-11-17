"""Recovery SAFE_MODE sends a safe_mode alert.

   Note: the old "disconnect" alert was fired only by the IBKR Gateway's own event
   wiring (app.ibkr.gateway.Gateway._on_disconnect), deleted with the rest of that
   tree — Tastytrade-only has no equivalent yet (a failed OAuth probe currently
   fails silently; see app/broker/status.py). Left for a future task to decide
   whether a probe-failure alert is wanted.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_aw.db python -m tests.test_alert_wiring
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine import recovery
        from app.services import alerts

        await init_db()
        seen = []
        async def cap(kind, msg, data): seen.append(kind)
        alerts.register_channel(cap)

        # A transmitted open local trade with no app-owned Alpaca position → reconcile
        # mismatch → SAFE_MODE.
        async with session_scope() as s:
            s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                              quantity=10, status="open", transmitted=True))

        async def empty_snapshot():
            return {"positions": [], "fills": [], "orders": [], "issues": []}

        recovery.fetch_snapshot = empty_snapshot

        ok = await recovery.run_recovery()
        assert ok is False
        assert "safe_mode" in seen, seen
        print("PASS: safe_mode alert fired")
    asyncio.run(_t())
    print("\nALL ALERT-WIRING TESTS PASSED")


if __name__ == "__main__":
    _run()
