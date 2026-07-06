# backend/tests/test_tc_contracts_step.py
import pytest

from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.params import TripleCalendarParams


class Src:
    def underlying_close(self, s, d): return 445.0
    def chain_strikes(self, u, e): return [float(r) for r in range(400, 491, 5)]
    def option_close(self, u, e, k, r, d): return 1.0 + 0.1 * max((e - d).days, 0)


class Exec:
    def __init__(self): self.calls = []
    async def __call__(self, underlying, legs, **kw):
        self.calls.append((kw.get("reason"), kw.get("action"), kw.get("quantity")))
        return {"transmitted": False, "paper": True}


@pytest.mark.asyncio
async def test_entry_stamps_and_orders_with_contracts():
    from datetime import date
    ex = Exec()
    out = await manage_step(date(2026, 6, 25), Src(), TripleCalendarParams(), ex,
                            position=None, contracts=4)
    assert out["action"] == "enter"
    assert out["position"].contracts == 4
    # the entry order was placed with quantity=4
    assert ("entry", "BUY", 4) in ex.calls
