"""Worker does not act while connection state is not READY.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_wp.db python -m tests.test_worker_pause
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db, session_scope
        from app.db.models import StrategyState
        from app.engine.manager import engine_manager
        from app.engine.worker import run_strategy
        from app.ibkr.gateway import ConnState, get_gateway
        from app.strategies.base import BaseStrategy, Candidate

        get_settings().scan_interval_seconds = 0
        get_gateway().set_state(ConnState.DISCONNECTED)   # NOT ready
        await init_db()
        async with session_scope() as s:
            s.add(StrategyState(key="paws", name="P", ready=True, status="idle"))

        scanned = {"n": 0}

        class S(BaseStrategy):
            key = "paws"; name = "P"; ready = True
            async def scan(self, universe):
                scanned["n"] += 1
                return [Candidate(symbol="AAPL", score=1.0, reason="x")]

        await engine_manager.start_worker("paws", run_strategy(S()))
        await asyncio.sleep(0.3)
        assert scanned["n"] == 0, f"worker scanned while not READY: {scanned}"
        async with session_scope() as s:
            st = await s.get(StrategyState, "paws")
            assert st.status == "paused", st.status
        await engine_manager.kill_all()
        print("PASS: worker paused while not READY")
    asyncio.run(_t())
    print("\nALL WORKER-PAUSE TESTS PASSED")


if __name__ == "__main__":
    _run()
