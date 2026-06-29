"""Rule-driven worker persists a WorkerState snapshot when it arms an entry.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_wsw.db python -m tests.test_worker_state_writes
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine import worker as W
        from app.engine.worker_state import load_state
        from app.strategies.ema_scalper.signals import Action, Pending, State

        await init_db()

        class Strat:
            key = "strategy_1"

        state = State()
        bars = [{"time": 1700000000000, "high": 101, "low": 99, "close": 100}]
        action = Action(kind="arm", side="long", price=101.0, stop=99.0, target=103.0,
                        entry_type="pin")

        def apply_action(a, st, idx, bar):
            st.pending = Pending(side=a.side, trigger=a.price, stop=a.stop, target=a.target,
                                 armed_index=idx, entry_type=a.entry_type)

        await W._handle_action(Strat(), "AAPL", action, state, bars, 0, None,
                               size_position=None, submit_equity_order=None,
                               apply_action=apply_action)
        snap = await load_state("strategy_1")
        assert snap is not None and snap["managed_symbol"] == "AAPL", snap
        assert snap["armed"]["side"] == "long" and snap["armed"]["trigger"] == 101.0, snap
        print("PASS: arm action persisted to WorkerState")
    asyncio.run(_t())
    print("\nALL WORKER-STATE-WRITE TESTS PASSED")


if __name__ == "__main__":
    _run()
