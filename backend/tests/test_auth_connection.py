import pytest

from app.auth.manager import session_manager
from app.ibkr.gateway import get_gateway


def test_is_authenticated_tracks_gateway(monkeypatch):
    monkeypatch.setattr(get_gateway(), "is_connected", lambda: False)
    assert session_manager.is_authenticated() is False
    monkeypatch.setattr(get_gateway(), "is_connected", lambda: True)
    assert session_manager.is_authenticated() is True


@pytest.mark.asyncio
async def test_require_auth_raises_when_disconnected(monkeypatch):
    from fastapi import HTTPException

    from app.auth.gate import require_auth
    monkeypatch.setattr(get_gateway(), "is_connected", lambda: False)
    with pytest.raises(HTTPException):
        await require_auth()


@pytest.mark.asyncio
async def test_require_auth_passes_when_connected(monkeypatch):
    from app.auth.gate import require_auth
    monkeypatch.setattr(get_gateway(), "is_connected", lambda: True)
    assert await require_auth() is None
