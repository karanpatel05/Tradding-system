import pytest

from app.db.base import init_db
from app.options import store

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0, "expiry": "2026-07-31", "qty": 1}]


@pytest.mark.asyncio
async def test_save_trade_stores_contracts():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25, contracts=3)
    assert (await store.get_trade(tid)).contracts == 3
    tid2 = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                  target_pct=None, stop_pct=None)
    assert (await store.get_trade(tid2)).contracts == 1  # default
