from datetime import date

import pytest

from app.db.base import init_db
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import (list_open, load_open, load_open_id,
                                                  save_position)

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


@pytest.mark.asyncio
async def test_open_lookups_key_by_underlying():
    await init_db()
    q = Position("QQQ", date(2026, 6, 25), [Calendar(445.0, SE, BE)], net_debit=1.5)
    s = Position("SPY", date(2026, 6, 25), [Calendar(560.0, SE, BE)], net_debit=2.5)
    qid = await save_position("strategy_2", q, order_ids=[], paper=True)
    sid = await save_position("strategy_2", s, order_ids=[], paper=True)

    # underlying-filtered lookups return the right one
    assert (await load_open("strategy_2", "QQQ")).underlying == "QQQ"
    assert (await load_open("strategy_2", "SPY")).underlying == "SPY"
    assert await load_open("strategy_2", "IWM") is None
    assert await load_open_id("strategy_2", "QQQ") == qid
    assert await load_open_id("strategy_2", "SPY") == sid

    # list_open returns all; no-arg load_open still returns *a* record (back-compat)
    opens = await list_open("strategy_2")
    assert {r.underlying for r in opens} == {"QQQ", "SPY"}
    assert await load_open("strategy_2") is not None
