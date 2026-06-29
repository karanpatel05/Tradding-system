"""backfill_active force-refreshes active strategies' specs via provider + cache.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_bf.db python -m tests.test_backfill_active
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import StrategyState
        from app.services import backfill
        from app.services import history_cache

        await init_db()
        async with session_scope() as s:
            s.add(StrategyState(key="strategy_1", name="Scalper", ready=True,
                                status="active", selected_symbol="AAPL"))

        calls = []

        class FakeProvider:
            name = "ibkr"
            async def get_bars(self, symbol, interval, start=None, end=None):
                calls.append((symbol, interval))
                return [{"time": 1, "close": 100}]

        class FakeStrat:
            key = "strategy_1"
            def backfill_specs(self, sel):
                return [(sel, "5m")] if sel else []

        saved = []
        backfill.get_historical_provider = lambda: FakeProvider()      # type: ignore[assignment]
        backfill.get_strategy = lambda key: FakeStrat()                # type: ignore[assignment]
        history_cache.save = lambda *a, **k: saved.append((a, k))      # type: ignore[assignment]

        n = await backfill.backfill_active(delay=0)
        assert n == 1 and calls == [("AAPL", "5m")], (n, calls)
        assert saved and saved[0][0][0] == "AAPL", saved
        print("PASS: backfill_active refreshed the active spec")
    asyncio.run(_t())
    print("\nALL BACKFILL-ACTIVE TESTS PASSED")


if __name__ == "__main__":
    _run()
