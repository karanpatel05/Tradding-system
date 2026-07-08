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
