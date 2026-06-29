"""Rule-driven startup rebuilds in-memory State from a persisted snapshot.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_wsr.db python -m tests.test_worker_state_rebuild
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import TradeRecord
        from app.engine.worker import _rebuild_rule_state
        from app.engine.worker_state import save_state

        await init_db()
        async with session_scope() as s:
            tr = TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                             quantity=10, entry_price=100.0, stop=99.0, target=103.0,
                             status="open")
            s.add(tr)
            await s.flush()
            tid = tr.id
        await save_state("strategy_1", managed_symbol="AAPL", armed=None,
                         open_trade_id=tid, last_bar_ms=1700000000000)

        rebuilt = await _rebuild_rule_state("strategy_1")
        assert rebuilt is not None, "expected a rebuilt snapshot"
        state, selected, open_qty, open_trade_id, last_bar_ms = rebuilt
        assert selected == "AAPL" and open_qty == 10 and open_trade_id == tid, rebuilt
        assert state.position is not None and state.position.side == "long", state.position
        assert state.position.stop == 99.0 and state.position.target == 103.0
        print("PASS: rebuilt open position from snapshot + trade record")
    asyncio.run(_t())
    print("\nALL WORKER-STATE-REBUILD TESTS PASSED")


if __name__ == "__main__":
    _run()
