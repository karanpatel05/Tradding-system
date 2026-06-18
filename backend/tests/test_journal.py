"""Calendar aggregation test. Run with a throwaway DB:
   DATABASE_URL=sqlite+aiosqlite:///./_test.db python -m tests.test_journal
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone


async def _run() -> None:
    from app.db.base import init_db, session_scope
    from app.db.models import TradeRecord
    from app.services.journal import get_calendar, get_day_detail

    await init_db()

    d1 = datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)
    d2 = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
    async with session_scope() as s:
        # Day 1: two wins + one loss => pnl 150, win_rate 2/3
        s.add(TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy",
                          quantity=1, pnl=100.0, exit_time=d1, status="closed"))
        s.add(TradeRecord(strategy_key="strategy_1", symbol="MSFT", side="buy",
                          quantity=1, pnl=100.0, exit_time=d1, status="closed"))
        s.add(TradeRecord(strategy_key="strategy_1", symbol="TSLA", side="buy",
                          quantity=1, pnl=-50.0, exit_time=d1, status="closed"))
        # Day 2: one loss
        s.add(TradeRecord(strategy_key="strategy_2", symbol="NVDA", side="sell",
                          quantity=1, pnl=-25.0, exit_time=d2, status="closed"))
        # An open trade taken on day 1 (entry_time set) — should appear by entry date,
        # but contribute nothing to realized P&L / win rate.
        s.add(TradeRecord(strategy_key="strategy_1", symbol="AMD", side="buy",
                          quantity=1, entry_price=100.0, entry_time=d1, status="open"))

    cal = await get_calendar(date(2026, 5, 1), date(2026, 5, 31))
    by_date = {row["date"]: row for row in cal}
    assert by_date["2026-05-01"]["realized_pnl"] == 150.0, by_date["2026-05-01"]
    assert by_date["2026-05-01"]["closed_count"] == 3, by_date["2026-05-01"]
    assert by_date["2026-05-01"]["open_count"] == 1, by_date["2026-05-01"]
    assert by_date["2026-05-01"]["trade_count"] == 4, by_date["2026-05-01"]
    assert by_date["2026-05-01"]["win_rate"] == round(2 / 3, 4)  # closed only
    assert by_date["2026-05-02"]["realized_pnl"] == -25.0
    print("PASS: calendar aggregation (closed P&L + open-by-entry count)")

    detail = await get_day_detail(date(2026, 5, 1))
    assert detail["realized_pnl"] == 150.0  # open trade adds no realized P&L
    assert len(detail["trades"]) == 4, detail["trades"]
    statuses = sorted(t["status"] for t in detail["trades"])
    assert statuses == ["closed", "closed", "closed", "open"], statuses
    assert any(t["symbol"] == "AMD" and t["status"] == "open" for t in detail["trades"])
    print("PASS: day detail includes the open trade taken that day")

    # Range filter excludes out-of-range days
    cal_narrow = await get_calendar(date(2026, 5, 2), date(2026, 5, 2))
    assert {r["date"] for r in cal_narrow} == {"2026-05-02"}, cal_narrow
    print("PASS: range filter")

    print("\nALL JOURNAL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
