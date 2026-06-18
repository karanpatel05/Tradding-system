import pytest

from app.config import get_settings
from app.engine import execution


@pytest.mark.asyncio
async def test_equity_order_paper_mode_does_not_transmit(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    res = await execution.submit_equity_order("NVDA", "enter_long", 10, reason="test")
    assert res["transmitted"] is False and res.get("paper") is True
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_bracket_paper_mode_returns_levels(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    res = await execution.submit_bracket_order("NVDA", "long", 10, 100.0, 95.0, 110.0)
    assert res["transmitted"] is False and res["stop"] == 95.0 and res["target"] == 110.0
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_zero_quantity_skips():
    res = await execution.submit_equity_order("NVDA", "enter_long", 0)
    assert res["transmitted"] is False


@pytest.mark.asyncio
async def test_unknown_intent_raises():
    with pytest.raises(ValueError):
        await execution.submit_equity_order("NVDA", "bogus", 5)


@pytest.mark.asyncio
async def test_cancel_paper_mode_noop(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    res = await execution.cancel_symbol_orders("NVDA")
    assert res["cancelled"] == 0 and res.get("paper") is True
    get_settings.cache_clear()
