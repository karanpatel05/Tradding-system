import pytest

from app.db.base import init_db
from app.options import store

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0, "expiry": "2026-07-31", "qty": 1}]


@pytest.mark.asyncio
async def test_save_list_value_close():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)
    assert (await store.get_trade(tid)).status == "open"
    assert any(t.id == tid for t in await store.list_open())

    await store.update_valuation(tid, value=2.2, pnl=20.0, pnl_pct=0.10)
    t = await store.get_trade(tid)
    assert t.last_value == 2.2 and t.last_pnl == 20.0 and t.valued_at is not None

    await store.close_trade(tid, pnl=20.0, reason="target")
    t = await store.get_trade(tid)
    assert t.status == "closed" and t.exit_reason == "target" and t.exit_time is not None
    assert all(o.id != tid for o in await store.list_open())
