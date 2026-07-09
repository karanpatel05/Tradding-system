"""The options-aware detail endpoint (/api/strategies/{key}/options-position) reads the
persisted OptionPositionRecord + the loop's cached valuation and derives DTE, target/stop
values, progress, and breach distances — with NO IBKR calls.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_opt.db python -m tests.test_tc_options_endpoint
"""
from datetime import date, timedelta

import pytest

from app.db.base import init_db
from app.routers.strategies import options_position
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import save_position, update_value

KEY = "strategy_2"


async def _reset(key: str) -> None:
    """Deterministic single-ticker (fallback) flat state regardless of a reused test DB file:
    clear tc_tickers so the endpoint falls back to the single tc_underlying, and close opens."""
    from app.db.base import session_scope
    from app.db.models import OptionPositionRecord, StrategyState
    from sqlalchemy import update
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st is None:
            st = StrategyState(key=key, name="Triple Calendar", ready=True)
            session.add(st)
        st.tc_tickers = []
        await session.execute(update(OptionPositionRecord)
                              .where(OptionPositionRecord.strategy_key == key,
                                     OptionPositionRecord.status == "open")
                              .values(status="closed"))


@pytest.mark.asyncio
async def test_flat_returns_no_position():
    await init_db()
    await _reset(KEY)
    out = await options_position(KEY)
    # One fallback ticker, flat.
    assert len(out["tickers"]) == 1 and out["tickers"][0]["has_position"] is False
    # status fields present even when flat, so the UI can render the status strip.
    assert "conn_ready" in out and "market_open" in out and "live_orders_enabled" in out
    assert out["tickers"][0]["ticker"]  # configured underlying surfaced


@pytest.mark.asyncio
async def test_open_position_derives_dte_targets_and_breach():
    await init_db()
    await _reset(KEY)
    today = date.today()
    sell = today + timedelta(days=21)
    buy = today + timedelta(days=28)
    pos = Position("QQQ", today, [
        Calendar(495.0, sell, buy),
        Calendar(500.0, sell, buy),
        Calendar(505.0, sell, buy),
    ], net_debit=4.00)
    rec_id = await save_position(KEY, pos, order_ids=[], paper=True)
    # Loop cached a valuation: value 4.20 → +5% of the 4.00 debit, spot inside the strikes.
    await update_value(rec_id, value=4.20, pnl_pct=0.05, spot=501.20)

    out = await options_position(KEY)
    # tc_tickers is empty → single fallback ticker (QQQ), which holds the open record.
    assert len(out["tickers"]) == 1
    t0 = out["tickers"][0]
    assert t0["has_position"] is True
    assert t0["underlying"] == "QQQ" and t0["net_debit"] == 4.0
    assert len(t0["calendars"]) == 3
    c = t0["calendars"][0]
    assert c["sell_dte"] == 21 and c["buy_dte"] == 28
    # +10% target / -25% stop off the 4.00 debit.
    assert t0["target_value"] == pytest.approx(4.40)
    assert t0["stop_value"] == pytest.approx(3.00)
    # value 4.20 → +$20/contract, 50% of the way to the +10% target, not near the stop.
    assert t0["unrealized_pnl"] == pytest.approx(20.0)
    assert t0["progress_to_target"] == pytest.approx(0.5)
    assert t0["distance_to_stop"] == pytest.approx(0.0)
    assert t0["stale"] is False
    # breach: 501.20 sits between the 495 lower and 505 upper strikes.
    b = t0["breach"]
    assert b["lower_strike"] == 495.0 and b["upper_strike"] == 505.0
    assert b["dist_lower_pct"] < 0 < b["dist_upper_pct"]


def _run() -> None:
    import asyncio
    asyncio.run(test_flat_returns_no_position())
    asyncio.run(test_open_position_derives_dte_targets_and_breach())
    print("ALL TC OPTIONS-ENDPOINT TESTS PASSED")


if __name__ == "__main__":
    _run()
