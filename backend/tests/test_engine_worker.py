"""Engine worker loop test: a ready strategy selects a stock, then cancels clean.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_test_worker.db python -m tests.test_engine_worker
"""

from __future__ import annotations

import asyncio


async def _run() -> None:
    from app.config import get_settings
    from app.db.base import init_db, session_scope
    from app.db.models import StrategyState
    from app.engine import worker as worker_mod
    from app.engine.manager import engine_manager
    from app.engine.worker import run_strategy
    from app.strategies.base import BaseStrategy, Candidate

    get_settings().scan_interval_seconds = 0  # tight loop for the test
    worker_mod.broker_ready = lambda: True  # workers only act when ready

    await init_db()
    async with session_scope() as s:
        s.add(StrategyState(key="test_strat", name="Test", ready=True, status="idle"))

    class FakeStrategy(BaseStrategy):
        key = "test_strat"
        name = "Test"
        ready = True

        async def scan(self, universe):
            return [Candidate(symbol="AAPL", score=0.9, reason="top pick")]

    await engine_manager.start_worker("test_strat", run_strategy(FakeStrategy()))

    # Poll until the worker records its selection.
    selected = None
    for _ in range(50):
        await asyncio.sleep(0.05)
        async with session_scope() as s:
            st = await s.get(StrategyState, "test_strat")
            selected = st.selected_symbol
        if selected:
            break
    assert selected == "AAPL", f"selected={selected}"
    assert engine_manager.is_running("test_strat")
    print("PASS: worker scanned and selected AAPL")

    # Kill switch cancels it cleanly.
    report = await engine_manager.kill_all()
    assert "test_strat" in report.tasks_cancelled, report.to_dict()
    assert not engine_manager.is_running("test_strat")
    print("PASS: worker cancelled cleanly by kill switch")

    print("\nALL ENGINE WORKER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
