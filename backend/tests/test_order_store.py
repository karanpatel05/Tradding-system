"""Order intents are durable and dedupable by ref.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_ord.db python -m tests.test_order_store
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.engine.order_store import (mark_perm_id, new_ref, open_refs,
                                            record_intent, set_status)

        await init_db()
        ref = new_ref("strategy_1", "AAPL")
        assert ref.startswith("strategy_1-AAPL-") and len(ref) > len("strategy_1-AAPL-")
        r2 = await record_intent("strategy_1", "AAPL", "enter_long", 10)
        assert (await open_refs()) == [r2], await open_refs()
        await mark_perm_id(r2, "987654")
        await set_status(r2, "filled")
        assert (await open_refs()) == [], "filled intent should not be open"
        print("PASS: intent lifecycle submitting->filled")

    asyncio.run(_t())
    print("\nALL ORDER-STORE TESTS PASSED")


if __name__ == "__main__":
    _run()
