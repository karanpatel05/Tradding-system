"""The application auth gate.

A single FastAPI dependency, :func:`require_auth`, that every protected REST
route depends on. The WebSocket endpoint reuses :func:`is_request_authenticated`
on connect so the push channel is gated too (not just HTTP).
"""

from __future__ import annotations

from fastapi import Cookie, HTTPException, status

from app.auth.manager import session_manager

# Name of the HttpOnly cookie carrying the opaque app session id.
APP_SESSION_COOKIE = "ts_session"


def is_authenticated(app_session_id: str | None) -> bool:
    """Pure check used by both the REST dependency and the WS handshake."""
    return session_manager.validate_cookie(app_session_id)


async def require_auth(ts_session: str | None = Cookie(default=None)) -> None:
    """REST dependency. Raises 401 unless the cookie maps to the active session."""
    if not is_authenticated(ts_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
