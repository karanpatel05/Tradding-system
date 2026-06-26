from datetime import date

import pytest

from app.config import get_settings
from app.engine import execution


@pytest.mark.asyncio
async def test_combo_paper_mode_does_not_transmit(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    legs = [{"expiry": date(2026, 7, 17), "strike": 445.0, "right": "C", "action": "SELL"},
            {"expiry": date(2026, 7, 24), "strike": 445.0, "right": "C", "action": "BUY"}]
    res = await execution.submit_combo_order("QQQ", legs, net_price=1.25, reason="entry")
    assert res["transmitted"] is False and res.get("paper") is True
    assert res["legs"] == 2 and "QQQ" in res["detail"]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_combo_empty_legs_skips():
    res = await execution.submit_combo_order("QQQ", [], net_price=1.0)
    assert res["transmitted"] is False
