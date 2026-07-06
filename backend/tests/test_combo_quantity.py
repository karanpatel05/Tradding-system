import pytest

from app.config import get_settings
from app.engine import execution


class _FakeIB:
    def __init__(self):
        self.placed = None

    async def qualifyContractsAsync(self, c):
        c.conId = 111  # pretend it resolved
        return [c]

    def placeOrder(self, contract, order):
        self.placed = order
        class _T:  # minimal trade stub
            class order:  # noqa: N801
                orderId = 7
        _T.order.orderId = 7
        return _T


@pytest.mark.asyncio
async def test_submit_combo_order_uses_quantity(monkeypatch):
    fake = _FakeIB()

    class _GW:
        def is_connected(self): return True
        async def get_ib(self): return fake

    monkeypatch.setattr(execution, "get_gateway", lambda: _GW())
    monkeypatch.setattr(get_settings(), "enable_live_orders", True, raising=False)

    from datetime import date
    legs = [{"expiry": date(2026, 7, 17), "strike": 500.0, "right": "C", "action": "BUY"}]
    out = await execution.submit_combo_order("QQQ", legs, net_price=4.2, action="BUY",
                                             reason="entry", quantity=3)
    assert out["transmitted"] is True
    assert fake.placed.totalQuantity == 3
