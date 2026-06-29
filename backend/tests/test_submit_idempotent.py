"""Live equity submit records a durable intent + sets orderRef before transmit.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_idem.db ENABLE_LIVE_ORDERS=1 python -m tests.test_submit_idempotent
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.config import get_settings
        from app.db.base import init_db
        from app.engine import execution
        from app.engine.order_store import open_refs

        get_settings().enable_live_orders = True
        await init_db()

        captured = {}

        class FakeOrder:
            def __init__(self, *a, **k):
                self.orderRef = None

        class FakeTrade:
            class order:  # noqa: N801
                permId = 555
                orderId = 1

        class FakeIB:
            def qualifyContractsAsync(self, *a):
                async def _n():
                    return a
                return _n()

            def placeOrder(self, contract, order):
                captured["orderRef"] = order.orderRef
                return FakeTrade()

        async def fake_get_ib():
            return FakeIB()

        execution.get_gateway().get_ib = fake_get_ib  # type: ignore[assignment]
        execution.MarketOrder = FakeOrder              # type: ignore[attr-defined]

        res = await execution.submit_equity_order("AAPL", "enter_long", 10, reason="entry")
        assert res["transmitted"] is True, res
        assert res["ref"] in (await open_refs()), res
        assert captured["orderRef"] == res["ref"], captured
        print("PASS: live submit set orderRef + recorded intent")

    asyncio.run(_t())
    print("\nALL SUBMIT-IDEMPOTENT TESTS PASSED")


if __name__ == "__main__":
    _run()
