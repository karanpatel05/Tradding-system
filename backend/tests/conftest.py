"""Suite-wide fixtures."""
import pytest

from app.services import market


@pytest.fixture(autouse=True)
def _never_write_real_env(monkeypatch, tmp_path):
    """The app's settings writers (order-routing switch, Tastytrade creds save) go to a
    temp file in every test, never backend/.env. Tests that need their own file
    monkeypatch over this."""
    from app.auth import routes as auth_routes
    from app.routers import system
    monkeypatch.setattr(system, "_ENV_PATH", tmp_path / "system.env")
    monkeypatch.setattr(auth_routes, "_ENV_PATH", tmp_path / "auth.env")


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


@pytest.fixture(autouse=True)
def _isolate_broker_probe():
    """pytest-asyncio gives each test its own loop: never let one test's in-flight
    Tastytrade probe (possibly a deliberately hung one) leak into the next."""
    from app.broker import status as st

    def _reset():
        for task in [st._inflight, *st._tasks]:
            if task is not None and not task.done():
                task.cancel()
        st._tasks.clear()
        st._inflight = None
        st._inflight_forced = False

    _reset()
    yield
    _reset()
