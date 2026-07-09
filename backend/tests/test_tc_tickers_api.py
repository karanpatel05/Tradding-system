import pytest

from app.db.base import init_db
from app.routers import strategies


@pytest.mark.asyncio
async def test_set_tickers_normalizes_and_persists():
    await init_db()
    body = strategies.TickersBody(tickers=[
        strategies.TickerItem(ticker="qqq", contracts=2),
        strategies.TickerItem(ticker="SPY", contracts=0),   # clamps to 1
        strategies.TickerItem(ticker="qqq", contracts=9),   # dup -> dropped (first wins)
        strategies.TickerItem(ticker="", contracts=1),      # blank -> dropped
    ])
    out = await strategies.set_tickers("strategy_2", body)
    assert out["tickers"] == [{"ticker": "QQQ", "contracts": 2}, {"ticker": "SPY", "contracts": 1}]
    assert (await strategies.get_tickers("strategy_2"))["tickers"] == out["tickers"]
    assert (await strategies.detail("strategy_2"))["tc_tickers"] == out["tickers"]


@pytest.mark.asyncio
async def test_set_tickers_rejects_dropping_ticker_with_open_position():
    """Removing a ticker that holds an open position would orphan it (the loop only manages
    configured tickers), so the PUT is rejected until the position exits."""
    from fastapi import HTTPException

    from app.db.base import session_scope
    from app.db.models import OptionPositionRecord

    await init_db()
    # Isolation-safe under a shared test DB: earlier tests may leave strategy_2 opens, which
    # would trip the orphan guard on our setup PUT — close them first.
    from sqlalchemy import update
    async with session_scope() as s:
        await s.execute(update(OptionPositionRecord)
                        .where(OptionPositionRecord.strategy_key == "strategy_2",
                               OptionPositionRecord.status == "open")
                        .values(status="closed"))
    await strategies.set_tickers("strategy_2", strategies.TickersBody(tickers=[
        strategies.TickerItem(ticker="QQQ", contracts=1),
        strategies.TickerItem(ticker="SPY", contracts=1),
    ]))
    async with session_scope() as s:
        s.add(OptionPositionRecord(strategy_key="strategy_2", underlying="SPY", status="open",
                                   paper=True, net_debit=2.0, contracts=1,
                                   calendars=[], order_ids=[]))
    # Dropping SPY (open) must 400; QQQ (flat) list without SPY is rejected.
    with pytest.raises(HTTPException) as ei:
        await strategies.set_tickers("strategy_2", strategies.TickersBody(tickers=[
            strategies.TickerItem(ticker="QQQ", contracts=1)]))
    assert ei.value.status_code == 400 and "SPY" in ei.value.detail
    # Keeping SPY in the list is fine (contracts change allowed).
    out = await strategies.set_tickers("strategy_2", strategies.TickersBody(tickers=[
        strategies.TickerItem(ticker="SPY", contracts=2)]))
    assert out["tickers"] == [{"ticker": "SPY", "contracts": 2}]
    # Cleanup: close the seeded record and clear tickers for other tests on a shared DB.
    async with session_scope() as s:
        await s.execute(update(OptionPositionRecord)
                        .where(OptionPositionRecord.strategy_key == "strategy_2",
                               OptionPositionRecord.status == "open")
                        .values(status="closed"))
    await strategies.set_tickers("strategy_2", strategies.TickersBody(tickers=[]))
