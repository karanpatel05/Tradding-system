"""Auth = the Tastytrade OAuth probe cache (see app.broker.status), not the IBKR
Gateway. Repointed from the old gateway-connected seam onto the probe cache that
session_manager.is_authenticated()/require_auth() now read."""
import pytest

from app.auth.manager import session_manager
from app.broker import status as st


def test_is_authenticated_tracks_probe_cache(monkeypatch):
    monkeypatch.setattr(st, "_cache", {"result": None, "at": 0.0})
    assert session_manager.is_authenticated() is False
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": 1e9})
    assert session_manager.is_authenticated() is True


@pytest.mark.asyncio
async def test_require_auth_raises_when_not_ready(monkeypatch):
    from fastapi import HTTPException

    from app.auth.gate import require_auth
    monkeypatch.setattr(st, "_cache", {"result": None, "at": 0.0})
    with pytest.raises(HTTPException):
        await require_auth()


@pytest.mark.asyncio
async def test_require_auth_passes_when_ready(monkeypatch):
    from app.auth.gate import require_auth
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": 1e9})
    assert await require_auth() is None
