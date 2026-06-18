"""The application auth gate.

A single FastAPI dependency, :func:`require_auth`, that every protected REST route
depends on. The WebSocket endpoint reuses :func:`is_authenticated` on connect so the
push channel is gated too. "Authenticated" now means the IBKR Gateway is connected
(no cookies / OAuth)."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.auth.manager import session_manager

# Retained for import compatibility with the WS handshake; no longer used to gate.
APP_SESSION_COOKIE = "ts_session"


def is_authenticated(app_session_id: str | None = None) -> bool:
    """Pure check used by both the REST dependency and the WS handshake. The
    ``app_session_id`` argument is ignored (kept for call-site compatibility) —
    auth is now purely 'is the IBKR Gateway connected?'."""
    return session_manager.is_authenticated()


async def require_auth() -> None:
    """REST dependency. Raises 401 unless the IBKR Gateway is connected."""
    if not session_manager.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not connected to IBKR Gateway",
        )
