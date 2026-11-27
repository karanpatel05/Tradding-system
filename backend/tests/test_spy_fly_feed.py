"""Offline SPY chain and quote tests using the Tastytrade SDK's option model."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from tastytrade import instruments

from app.strategies.xsp_iron_fly.feed import TastytradeFeed
from app.tastytrade import session

EXPIRY = date(2026, 11, 20)
CLOSE = datetime(2026, 11, 20, 21, tzinfo=timezone.utc)


def _option(right, strike, **overrides):
    data = {
        "symbol": f"SPY   261120{right}{int(strike * 1000):08d}",
        "instrument_type": "Equity Option", "active": True, "root_symbol": "SPY",
        "underlying_symbol": "SPY", "expiration_date": EXPIRY, "exercise_style": "American",
        "option_type": right, "shares_per_contract": 100, "strike_price": strike,
        "settlement_type": "PM", "option_chain_type": "Standard", "expiration_type": "Weekly",
        "stops_trading_at": CLOSE, "market_time_instrument_collection": "US Options",
        "days_to_expiration": 0, "is_closing_only": False,
    }
    return instruments.Option(**{**data, **overrides})


@pytest.mark.asyncio
async def test_spy_chain_selects_standard_0dte_contracts_and_keeps_provider_symbols(monkeypatch):
    other_expiry = date(2026, 11, 23)
    chain = {
        EXPIRY: [
            _option("P", 600), _option("C", 600.5),
            _option("P", 601, root_symbol="SPY1"),
            _option("P", 602, underlying_symbol="XSP"),
            _option("P", 603, shares_per_contract=10),
            _option("P", 604, option_chain_type="Non-Standard"),
            _option("P", 605, settlement_type="AM"),
            _option("P", 606, active=False),
            _option("P", 607, is_closing_only=True),
        ],
        other_expiry: [_option("C", 610, expiration_date=other_expiry)],
    }
    mock_session = object()
    fetch = AsyncMock(return_value=chain)
    monkeypatch.setattr(session, "get_tt_session", AsyncMock(return_value=mock_session))
    monkeypatch.setattr(instruments, "get_option_chain", fetch)
    feed = TastytradeFeed()
    contracts = await feed.contracts("SPY", EXPIRY)
    assert [(c.right, c.strike, c.symbol, c.occ, c.stops_trading_at) for c in contracts] == [
        ("P", 600.0, ".SPY261120P600", "SPY   261120P00600000", CLOSE),
        ("C", 600.5, ".SPY261120C600.5", "SPY   261120C00600500", CLOSE),
    ]
    assert await feed.contracts("SPY", EXPIRY) == contracts
    fetch.assert_awaited_once_with(mock_session, "SPY")


@pytest.mark.asyncio
async def test_spy_uses_its_equity_trade_and_each_option_uses_its_own_quote(monkeypatch):
    symbol = ".SPY261120C600.5"
    feed = TastytradeFeed()
    streamer = Mock()
    streamer.trade_price = AsyncMock()
    streamer.quote = AsyncMock()
    streamer.cached_trade.return_value = (600.48, CLOSE.timestamp())
    streamer.cached_quote.return_value = ({"bid": 1.23, "ask": 1.29}, CLOSE.timestamp())
    monkeypatch.setattr(feed, "_streamer", lambda: streamer)
    await feed.subscribe(["SPY", symbol])
    streamer.trade_price.assert_awaited_once_with("SPY", timeout=0.0)
    streamer.quote.assert_awaited_once_with(symbol, timeout=0.0)
    spot, quote = feed.spot("SPY"), feed.quote(symbol)
    assert (spot.price, spot.ts) == (600.48, CLOSE)
    assert (quote.bid, quote.ask, quote.ts) == (1.23, 1.29, CLOSE)
    streamer.cached_trade.assert_called_once_with("SPY")
    streamer.cached_quote.assert_called_once_with(symbol)


@pytest.mark.asyncio
async def test_spy_close_cannot_supply_cash_settlement(monkeypatch):
    from app.marketdata import facade

    feed = TastytradeFeed()
    stock_price = Mock()
    daily_bars = AsyncMock()
    monkeypatch.setattr(feed, "spot", stock_price)
    monkeypatch.setattr(facade, "bars", daily_bars)
    assert await feed.settlement_price("SPY", EXPIRY, CLOSE, CLOSE) is None
    stock_price.assert_not_called()
    daily_bars.assert_not_awaited()
