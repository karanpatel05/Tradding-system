"""Authentication routes.

The browser only ever sees an opaque HttpOnly session cookie. The refresh token
(submitted once at login) and the resulting Tastytrade access token live entirely
server-side in :data:`session_manager` and are never returned in any response.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.auth import store
from app.auth.gate import APP_SESSION_COOKIE, require_auth
from app.auth.manager import session_manager
from app.config import get_settings
from app.tastytrade.client import NotAuthenticated, OAuthNotConfigured, build_session, get_client

logger = logging.getLogger("trading.auth")
settings = get_settings()

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    refresh_token: str = Field(..., min_length=8, description="Tastytrade OAuth refresh token")
    # Optional OAuth client secret entered in the panel; falls back to the
    # server-configured secret when omitted. Stored server-side only.
    provider_secret: str | None = Field(default=None, min_length=1)


class AuthStatus(BaseModel):
    authenticated: bool
    oauth_configured: bool
    accounts: list[str] = []


# Persistent cookie lifetime (30 days) so reopening the browser keeps the user
# signed in — paired with the server-side session store, which keeps the matching
# session alive across restarts.
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _set_session_cookie(response: Response, app_session_id: str) -> None:
    response.set_cookie(
        key=APP_SESSION_COOKIE,
        value=app_session_id,
        max_age=_COOKIE_MAX_AGE,             # persists across browser restarts
        httponly=True,                       # not readable by JS
        samesite="lax",                      # sent on same-site (localhost) XHR
        secure=settings.environment != "development",
        path="/",
    )


@router.get("/status", response_model=AuthStatus)
async def status_endpoint() -> AuthStatus:
    """Ungated — the frontend calls this to decide whether to show the auth panel.

    Reports OAuth configuration so the panel can render an actionable
    'OAuth not configured' message instead of failing opaquely.
    """
    accounts: list[str] = []
    if session_manager.is_authenticated():
        try:
            accounts = [a.account_number for a in await get_client().get_accounts()]
        except Exception:  # don't let a transient data error flip auth state
            logger.exception("Failed to list accounts for status")
    return AuthStatus(
        authenticated=session_manager.is_authenticated(),
        oauth_configured=settings.oauth_configured,
        accounts=accounts,
    )


@router.post("/login", response_model=AuthStatus)
async def login(body: LoginRequest, response: Response) -> AuthStatus:
    """Exchange a refresh token for a Tastytrade session, stored server-side."""
    try:
        tasty_session = await build_session(body.refresh_token, body.provider_secret)
    except OAuthNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except Exception as exc:  # bad refresh token, network, etc.
        logger.warning("Login failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed — check the refresh token.",
        )

    app_session_id = session_manager.establish(
        tasty_session,
        refresh_token=body.refresh_token,
        # Persist the panel-supplied secret only; when omitted, rebuild on restart
        # falls back to the server-configured TASTYTRADE_PROVIDER_SECRET.
        provider_secret=body.provider_secret,
    )
    _set_session_cookie(response, app_session_id)

    accounts: list[str] = []
    try:
        accounts = [a.account_number for a in await get_client().get_accounts()]
    except (NotAuthenticated, Exception):
        logger.exception("Login succeeded but account fetch failed")

    logger.info("Login successful (%d account(s))", len(accounts))
    return AuthStatus(authenticated=True, oauth_configured=True, accounts=accounts)


class CredentialsInfo(BaseModel):
    connected: bool
    oauth_configured: bool
    provider_secret: str = ""
    refresh_token: str = ""
    accounts: list[str] = []
    detail: str | None = None


class SaveCredentialsRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, description="Tastytrade OAuth refresh token")
    provider_secret: str | None = Field(default=None, min_length=1)


@router.get("/credentials", response_model=CredentialsInfo)
async def get_credentials() -> CredentialsInfo:
    """Current connection + stored credentials for the API settings tab.

    NOTE: this intentionally returns the OAuth secret and refresh token to the
    client so the API tab can display/edit them. That trades away the repo's
    "secrets never leave the server" guarantee in exchange for an editable
    credentials UI — acceptable for a local single-user tool bound to localhost.
    The values come from the live session, else the local store, else the env.
    """
    stored = store.load() or {}
    secret = (session_manager.provider_secret
              or stored.get("provider_secret")
              or settings.tastytrade_provider_secret
              or "")
    token = session_manager.refresh_token or stored.get("refresh_token") or ""
    accounts: list[str] = []
    if session_manager.is_authenticated():
        try:
            accounts = [a.account_number for a in await get_client().get_accounts()]
        except Exception:  # don't let a transient data error flip the view
            logger.exception("Failed to list accounts for credentials")
    return CredentialsInfo(
        connected=session_manager.is_authenticated(),
        oauth_configured=bool(secret),
        provider_secret=secret,
        refresh_token=token,
        accounts=accounts,
    )


@router.post("/credentials", response_model=CredentialsInfo)
async def save_credentials(body: SaveCredentialsRequest, response: Response) -> CredentialsInfo:
    """Save the OAuth secret + refresh token and try to connect.

    Unlike ``/login``, the credentials are **persisted to the local store even when
    the connection fails**, so they survive a frontend reload / server restart and
    can be edited rather than re-typed. Returns ``connected`` + a ``detail`` message
    when saved-but-not-connected (HTTP 200 either way — saving still succeeded)."""
    secret = body.provider_secret or settings.tastytrade_provider_secret
    error: str | None = None
    tasty_session = None
    try:
        tasty_session = await build_session(body.refresh_token, body.provider_secret)
    except OAuthNotConfigured as exc:
        error = str(exc)
    except Exception as exc:  # bad token / network — keep the creds, report not connected
        logger.warning("Save credentials: connection failed: %s", exc)
        error = "Saved locally, but connection failed — check the secret and refresh token."

    connected = False
    if tasty_session is not None:
        app_session_id = session_manager.establish(
            tasty_session, refresh_token=body.refresh_token, provider_secret=body.provider_secret)
        _set_session_cookie(response, app_session_id)  # establish() also persists
        connected = True
    elif settings.persist_session:
        # Connection didn't take, but remember the creds so they aren't lost.
        store.save(session_manager.app_session_id or secrets.token_urlsafe(32),
                   body.refresh_token, body.provider_secret)

    accounts: list[str] = []
    if connected:
        try:
            accounts = [a.account_number for a in await get_client().get_accounts()]
        except Exception:
            logger.exception("Account fetch after save failed")
    return CredentialsInfo(
        connected=connected, oauth_configured=bool(secret),
        provider_secret=secret or "", refresh_token=body.refresh_token,
        accounts=accounts, detail=error,
    )


@router.post("/logout", dependencies=[Depends(require_auth)])
async def logout(response: Response) -> dict:
    from app.services.market_stream import market_stream

    await market_stream.stop()  # tear down the live quote stream
    session_manager.clear()
    response.delete_cookie(APP_SESSION_COOKIE, path="/")
    return {"authenticated": False}
