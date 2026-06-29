"""fetch_snapshot normalizes IBKR positions/fills/orders.
   Run: python -m tests.test_recon_snapshot
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.engine.reconciliation import fetch_snapshot

    class C:
        def __init__(self, sym): self.symbol = sym; self.secType = "STK"
    class Pos:
        def __init__(self, sym, q, c): self.contract = C(sym); self.position = q; self.avgCost = c
    class Exec:
        def __init__(self): self.shares = 5; self.price = 101.0; self.permId = 7; self.execId = "e1"
    class Fill:
        def __init__(self, sym): self.contract = C(sym); self.execution = Exec()
    class Order:
        def __init__(self): self.permId = 7; self.orderRef = "strategy_1-AAPL-abc"
    class OrderStatus:
        status = "Submitted"
    class Trade:
        def __init__(self, sym): self.contract = C(sym); self.order = Order(); self.orderStatus = OrderStatus()

    class FakeIB:
        async def reqPositionsAsync(self): return [Pos("AAPL", 10, 1000.0)]
        async def reqExecutionsAsync(self, flt): return [Fill("AAPL")]
        async def reqAllOpenOrdersAsync(self): return [Trade("AAPL")]

    async def _t():
        snap = await fetch_snapshot(FakeIB())
        assert snap["positions"][0].symbol == "AAPL" and snap["positions"][0].qty == 10
        assert snap["fills"][0].qty == 5 and snap["fills"][0].perm_id == "7"
        assert snap["orders"][0].order_ref == "strategy_1-AAPL-abc"
    asyncio.run(_t())
    print("PASS: snapshot normalized")
    print("\nALL RECON-SNAPSHOT TESTS PASSED")


if __name__ == "__main__":
    _run()
