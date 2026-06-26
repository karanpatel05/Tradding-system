from datetime import date

import pytest

from app.db.base import init_db
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import close_position, load_open, save_position

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


@pytest.mark.asyncio
async def test_save_load_close_round_trip():
    await init_db()
    pos = Position("QQQ", date(2026, 6, 25), [Calendar(445.0, SE, BE)], net_debit=1.5)
    rec_id = await save_position("strategy_tc_test", pos, order_ids=[7], paper=True)
    loaded = await load_open("strategy_tc_test")
    assert loaded is not None and loaded.underlying == "QQQ"
    assert len(loaded.calendars) == 1 and loaded.calendars[0].strike == 445.0
    assert loaded.calendars[0].sell_expiry == SE
    await close_position(rec_id, pnl=0.2)
    assert await load_open("strategy_tc_test") is None
