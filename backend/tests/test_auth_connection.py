"""``is_authenticated`` = the Tastytrade OAuth probe cache (see app.broker.status): a
BROKER-readiness check, not a caller check. ``require_auth`` no longer reads the probe —
it authenticates the caller via the app token (covered in test_app_auth_token.py); the
test here just pins that the probe no longer authorizes callers."""
import pytest

from app.auth.manager import session_manager
from app.broker import status as st
from app.config import get_settings


def test_is_authenticated_tracks_probe_cache(monkeypatch):
    monkeypatch.setattr(st, "_cache", {"result": None, "at": 0.0})
    assert session_manager.is_authenticated() is False
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": 1e9})
    assert session_manager.is_authenticated() is True


@pytest.mark.asyncio
async def test_require_auth_ignores_probe_cache(monkeypatch):
    """A 'ready' broker probe must NOT authorize an anonymous caller."""
    from fastapi import HTTPException

    from app.auth.gate import require_auth
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": 1e9})  # probe ready
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        with pytest.raises(HTTPException):
            await require_auth(x_app_token=None)
    finally:
        s.app_auth_token = old
