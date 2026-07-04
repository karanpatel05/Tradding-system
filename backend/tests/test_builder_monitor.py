import pytest

from app.db.base import init_db
from app.options import monitor, store

LEGS = [{"action": "BUY", "right": "C", "strike": 100.0, "expiry": "2026-07-31", "qty": 1},
        {"action": "SELL", "right": "C", "strike": 105.0, "expiry": "2026-07-31", "qty": 1}]


def _mids(value):
    # a mid source that makes combo_value == `value` for the 2-leg (buy/sell) structure.
    async def src(underlying, legs):
        return [value, 0.0]   # BUY leg mid = value, SELL leg mid = 0 -> combo_value = value
    return src


@pytest.mark.asyncio
async def test_updates_pnl_then_hits_target():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)
    # value 2.1 -> +5%: updates, stays open.
    await monitor.value_open_trades_once(_mids(2.1))
    t = await store.get_trade(tid)
    assert t.status == "open" and t.last_pnl == pytest.approx(10.0)  # (2.1-2.0)*100
    # value 2.25 -> +12.5% >= target -> auto-close 'target'.
    await monitor.value_open_trades_once(_mids(2.25))
    t = await store.get_trade(tid)
    assert t.status == "closed" and t.exit_reason == "target"
    assert t.pnl == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_hits_stop():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)
    await monitor.value_open_trades_once(_mids(1.4))   # -30% <= -25% stop
    t = await store.get_trade(tid)
    assert t.status == "closed" and t.exit_reason == "stop"


@pytest.mark.asyncio
async def test_missing_mid_skips():
    await init_db()
    tid = await store.save_trade("QQQ", LEGS, net_debit=2.0, iv=0.25,
                                 target_pct=0.10, stop_pct=0.25)

    async def missing(underlying, legs):
        return [None, 0.0]
    await monitor.value_open_trades_once(missing)
    t = await store.get_trade(tid)
    assert t.status == "open" and t.last_value is None
