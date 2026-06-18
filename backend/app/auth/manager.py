"""Server-side session manager for Tastytrade OAuth.

Holds the live Tastytrade :class:`~tastytrade.Session` **server-side only**. The
refresh token, access/session token, and provider secret never leave this
process boundary — no API response model includes them.

A single app session id (opaque random token) is handed to the browser as an
HttpOnly cookie and mapped here to the authenticated Tastytrade session. This
keeps the trading credentials off the client entirely.

The token-exchange / refresh logic is wired in the auth slice; this module owns
the storage + lifecycle so the auth gate has a stable thing to depend on.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.auth import store


@dataclass
class _AuthState:
    app_session_id: str
    tasty_session: object  # tastytrade.Session — kept as object to avoid import at boot
    refresh_token: str | None = None
    provider_secret: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionManager:
    """Process-wide singleton holding the current authenticated session."""

    def __init__(self) -> None:
        self._state: _AuthState | None = None

    # --- state queries ---------------------------------------------------
    def is_authenticated(self) -> bool:
        return self._state is not None

    def validate_cookie(self, app_session_id: str | None) -> bool:
        """True only if the cookie matches the active server-side session."""
        if not app_session_id or self._state is None:
            return False
        return secrets.compare_digest(app_session_id, self._state.app_session_id)

    @property
    def app_session_id(self) -> str | None:
        return self._state.app_session_id if self._state else None

    @property
    def tasty_session(self) -> object | None:
        return self._state.tasty_session if self._state else None

    @property
    def refresh_token(self) -> str | None:
        return self._state.refresh_token if self._state else None

    @property
    def provider_secret(self) -> str | None:
        return self._state.provider_secret if self._state else None

    # --- lifecycle -------------------------------------------------------
    def establish(
        self,
        tasty_session: object,
        *,
        refresh_token: str | None = None,
        provider_secret: str | None = None,
        app_session_id: str | None = None,
        persist: bool = True,
    ) -> str:
        """Store an authenticated Tastytrade session; return the app session id
        to set as the browser cookie.

        ``app_session_id`` is supplied when restoring a persisted session so the
        existing browser cookie keeps validating across restarts; otherwise a
        fresh opaque id is minted. When ``persist`` and a refresh token is given,
        the credentials are written to the durable store so the session survives
        the next restart.
        """
        app_session_id = app_session_id or secrets.token_urlsafe(32)
        self._state = _AuthState(
            app_session_id=app_session_id,
            tasty_session=tasty_session,
            refresh_token=refresh_token,
            provider_secret=provider_secret,
        )
        if persist and refresh_token:
            store.save(app_session_id, refresh_token, provider_secret)
        return app_session_id

    def clear(self) -> None:
        """Drop the in-memory session and the persisted credentials."""
        self._state = None
        store.clear()


# Module-level singleton.
session_manager = SessionManager()
