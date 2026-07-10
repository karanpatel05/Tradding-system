"""Session manager: no creds -> None (unavailable); creds -> constructs the SDK Session
lazily and caches it; reset clears the cache. The SDK Session is faked - no network."""
import pytest

from app.config import get_settings
from app.tastytrade import session as tt_session


class _FakeSession:
    def __init__(self, provider_secret=None, refresh_token=None, is_test=False):
        self.provider_secret = provider_secret
        self.refresh_token = refresh_token


@pytest.mark.asyncio
async def test_no_creds_returns_none(monkeypatch):
    monkeypatch.setattr(get_settings(), "tt_provider_secret", "")
    monkeypatch.setattr(get_settings(), "tt_refresh_token", "")
    tt_session.reset_tt_session()
    assert await tt_session.get_tt_session() is None
    assert tt_session.tt_configured() is False


@pytest.mark.asyncio
async def test_creds_construct_and_cache(monkeypatch):
    monkeypatch.setattr(get_settings(), "tt_provider_secret", "sec")
    monkeypatch.setattr(get_settings(), "tt_refresh_token", "ref")
    monkeypatch.setattr(tt_session, "Session", _FakeSession)
    tt_session.reset_tt_session()
    s1 = await tt_session.get_tt_session()
    s2 = await tt_session.get_tt_session()
    assert s1 is s2 and s1.provider_secret == "sec" and s1.refresh_token == "ref"
    assert tt_session.tt_configured() is True
    tt_session.reset_tt_session()
    assert (await tt_session.get_tt_session()) is not s1
