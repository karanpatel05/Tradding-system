import pytest

from app.db.base import init_db, session_scope
from app.db.models import BuilderPositionRecord


@pytest.mark.asyncio
async def test_builder_record_round_trips():
    await init_db()  # create_all makes the new table; no migration needed
    async with session_scope() as s:
        rec = BuilderPositionRecord(
            underlying="QQQ",
            legs=[{"action": "BUY", "right": "C", "strike": 500.0,
                   "expiry": "2026-07-31", "qty": 1}],
            net_debit=4.2, iv=0.25, target_pct=0.10, stop_pct=0.25)
        s.add(rec)
        await s.flush()
        rid = rec.id
    async with session_scope() as s:
        got = await s.get(BuilderPositionRecord, rid)
        assert got.underlying == "QQQ" and got.status == "open"
        assert got.legs[0]["strike"] == 500.0 and got.net_debit == 4.2
