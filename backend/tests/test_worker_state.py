"""Worker state snapshot round-trips across a simulated restart.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_ws.db python -m tests.test_worker_state
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.worker_state import load_state, save_state

        await init_db()
        assert await load_state("strategy_1") is None
        await save_state("strategy_1", managed_symbol="AAPL",
                         armed={"side": "long", "price": 100.0, "stop": 99.0, "target": 102.0},
                         open_trade_id=42, last_bar_ms=1700000000000)
        snap = await load_state("strategy_1")
        assert snap["managed_symbol"] == "AAPL" and snap["open_trade_id"] == 42
        assert snap["armed"]["target"] == 102.0
        # upsert: update one field, others persist
        await save_state("strategy_1", last_bar_ms=1700000300000)
        snap = await load_state("strategy_1")
        assert snap["last_bar_ms"] == 1700000300000 and snap["managed_symbol"] == "AAPL"
        print("PASS: worker-state upsert round-trip")
    asyncio.run(_t())
    print("\nALL WORKER-STATE TESTS PASSED")


if __name__ == "__main__":
    _run()
