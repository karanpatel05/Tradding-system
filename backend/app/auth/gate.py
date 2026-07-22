"""The application auth gate.

Two distinct checks, deliberately kept apart:

* :func:`is_authenticated` — "does the SERVER's Tastytrade OAuth pair verify?" (a
  broker-readiness probe). Used by the WS handshake and by snapshot code to decide
  whether it can fetch live broker data. This is NOT a check on the caller.
* :func:`require_auth` — the REST dependency that authenticates the CALLER via a shared
  app token (``X-App-Token`` vs ``settings.app_auth_token``). Applied only to the
  dangerous mutating routes (arm live orders, kill, activate, resume, credential-write).
  Reads stay open so the dashboard still lands without a login (the deliberate no-gate UX).

FAIL CLOSED: while ``app_auth_token`` is empty, ``require_auth`` denies every caller —
set ``APP_AUTH_TOKEN`` in ``backend/.env`` (and paste it in the API tab) to enable the
gated actions."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.auth.manager import session_manager
from app.config import get_settings

# Retained for import compatibility with the WS handshake; no longer used to gate.
APP_SESSION_COOKIE = "ts_session"


def is_authenticated(app_session_id: str | None = None) -> bool:
    """Pure BROKER-readiness check (not a caller check). Used by the WS handshake and
    snapshot fetchers. The ``app_session_id`` argument is ignored (kept for call-site
    compatibility) — it reports 'does the Tastytrade OAuth pair verify?'."""
    return session_manager.is_authenticated()


async def require_auth(x_app_token: str | None = Header(default=None)) -> None:
    """REST dependency authenticating the caller. Raises 401 unless ``X-App-Token``
    matches the configured ``app_auth_token``. Fail closed when none is configured."""
    expected = get_settings().app_auth_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="App auth not configured — set APP_AUTH_TOKEN to enable this action",
        )
    if not x_app_token or not secrets.compare_digest(x_app_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid app token",
        )
