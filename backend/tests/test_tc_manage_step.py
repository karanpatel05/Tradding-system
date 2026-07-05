from datetime import date

import pytest

from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.params import TripleCalendarParams


class Src:
    def __init__(self, spot=445.0):
        self.spot = spot

    def underlying_close(self, sym, day):
        return self.spot

    def chain_strikes(self, u, e):
        return [float(r) for r in range(400, 491, 5)]

    def option_close(self, u, expiry, strike, right, day):
        return 1.0 + 0.1 * max((expiry - day).days, 0)


class Exec:
    def __init__(self):
        self.calls = []

    async def __call__(self, underlying, legs, **kw):
        self.calls.append((kw.get("reason"), len(legs)))
        return {"transmitted": False, "paper": True, "order_id": None}


@pytest.mark.asyncio
async def test_manage_step_enters_when_flat():
    ex = Exec()
    out = await manage_step(date(2026, 6, 25), Src(), TripleCalendarParams(), ex, position=None)
    assert out["action"] == "enter" and out["position"] is not None
    assert ex.calls and ex.calls[0][1] == 6   # 6-leg entry combo


@pytest.mark.asyncio
async def test_manage_step_holds_or_acts_when_open():
    ex = Exec()
    p = TripleCalendarParams()
    out = await manage_step(date(2026, 6, 25), Src(), p, ex, position=None)
    pos = out["position"]
    out2 = await manage_step(date(2026, 6, 26), Src(), p, ex, position=pos)
    assert out2["action"] in ("hold", "adjust", "exit", "roll")
