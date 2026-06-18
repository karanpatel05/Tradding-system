"""Kill switch must flatten the *ledger*, not just workers/broker.

The dashboard's Open Positions list is built primarily from open TradeRecords
(_strategy_positions_snapshot). A kill switch that cancels workers + flattens the
broker but leaves TradeRecords status=="open" leaves the dashboard showing stale
positions forever (guaranteed in paper mode, where there's no broker position at
all). This test pins the fix: close_open_trades() closes every open record.

Run: DATABASE_URL=sqlite+aiosqlite:///./_test_kill_ledger.db python -m tests.test_kill_flatten_ledger
"""

from __future__ import annotations

import asyncio


async def _run() -> None:
    from app.db.base import init_db, session_scope
    from app.db.models import TradeRecord
    from app.engine.worker import close_open_trades
    from app.routers.ws import _strategy_positions_snapshot

    await init_db()
    async with session_scope() as s:
        s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                          quantity=10, entry_price=100.0, status="open"))
        s.add(TradeRecord(strategy_key="strategy_1", symbol="MSFT", side="sell",
                          quantity=5, entry_price=200.0, status="open"))

    # Precondition: dashboard sees both open positions.
    snap = await _strategy_positions_snapshot()
    assert len(snap) == 2, snap
    print("PASS: dashboard shows 2 open positions before kill")

    # Kill: close every open trade.
    closed = await close_open_trades()
    assert closed == 2, closed
    print("PASS: close_open_trades() closed 2 records")

    # Postcondition: dashboard is flat, rows are closed with exit_time stamped and
    # no fabricated P&L (force-flatten has no captured fill).
    snap_after = await _strategy_positions_snapshot()
    assert snap_after == [], snap_after
    async with session_scope() as s:
        from sqlalchemy import select
        rows = (await s.execute(select(TradeRecord))).scalars().all()
        assert all(r.status == "closed" for r in rows), [r.status for r in rows]
        assert all(r.exit_time is not None for r in rows)
        assert all(r.pnl is None and r.exit_price is None for r in rows)
    print("PASS: dashboard flat after kill; records closed, P&L left unknown")

    # Idempotent: a second call closes nothing and doesn't error.
    assert await close_open_trades() == 0
    print("PASS: idempotent on already-flat ledger")

    print("\nALL KILL-FLATTEN-LEDGER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
