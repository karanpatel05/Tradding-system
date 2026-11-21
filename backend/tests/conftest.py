"""Suite-wide fixtures."""
import pytest

from app.services import market


class _EmptyHistory:
    async def get_bars(self, *args, **kwargs):
        return []


@pytest.fixture(autouse=True)
def _isolate_weekly_refresh(monkeypatch):
    """Reading the overview's weekly % schedules a background candle refresh. Keep
    it off the real feed and stop it leaking a pending task into the next test.
    Tests that need a specific provider monkeypatch over this one."""
    market._weekly_cache.clear()
    market._weekly_task = None
    monkeypatch.setattr(market, "_history_provider", lambda: _EmptyHistory())
    yield
    task = market._weekly_task
    if task is not None and not task.done():
        task.cancel()
    market._weekly_cache.clear()
    market._weekly_task = None
