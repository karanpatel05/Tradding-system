"""Journal calendar/day-detail include options P&L: TC records convert per-share pnl
to dollars (x100 x contracts); Builder records are already dollars; open option
positions count on their entry day; unclosed/no-exit rows never pollute realized."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.db.base import init_db, session_scope
from app.db.models import BuilderPositionRecord, OptionPositionRecord
from app.services.journal import get_calendar, get_day_detail

DAY = date(2026, 3, 3)          # a quiet historical day: no real records collide
TS = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_options_pnl_in_calendar_and_day():
    await init_db()
    async with session_scope() as s:
        s.add(OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="closed", paper=True,
            net_debit=4.0, contracts=2, calendars=[], order_ids=[],
            entry_time=TS - timedelta(days=3), exit_time=TS, pnl=0.53))  # per-share
        s.add(OptionPositionRecord(
            strategy_key="strategy_2", underlying="SPY", status="open", paper=True,
            net_debit=5.0, contracts=1, calendars=[], order_ids=[],
            entry_time=TS))                                              # open today
        s.add(BuilderPositionRecord(
            underlying="IWM", status="closed", net_debit=2.0, contracts=1,
            legs=[], iv=0.25, entry_time=TS - timedelta(days=1), exit_time=TS,
            pnl=-60.0))                                                  # dollars

    days = {d["date"]: d for d in await get_calendar(DAY - timedelta(days=5), DAY)}
    row = days[DAY.isoformat()]
    # TC: 0.53 * 100 * 2 = 106.0 ; Builder: -60.0 -> total 46.0
    assert row["realized_pnl"] == pytest.approx(46.0)
    assert row["closed_count"] >= 2 and row["open_count"] >= 1
    assert row["win_count"] >= 1

    detail = await get_day_detail(DAY)
    assert detail["realized_pnl"] == pytest.approx(46.0)
    opts = {(o["source"], o["underlying"]): o for o in detail["option_trades"]}
    assert opts[("triple_calendar", "QQQ")]["pnl"] == pytest.approx(106.0)
    assert opts[("builder", "IWM")]["pnl"] == pytest.approx(-60.0)
    assert opts[("triple_calendar", "SPY")]["status"] == "open"
