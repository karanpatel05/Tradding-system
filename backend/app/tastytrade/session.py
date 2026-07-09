"""Cached Tastytrade OAuth session. The SDK's Session fetches and auto-refreshes its
short-lived access token internally, so construction is cheap and there is no daily
login. Missing/blank creds -> None; the facade treats that as 'Tastytrade unavailable'."""
from __future__ import annotations

import asyncio

from tastytrade import Session

from app.config import get_settings

_session: Session | None = None
_lock = asyncio.Lock()


def tt_configured() -> bool:
    s = get_settings()
    return bool(s.tt_provider_secret and s.tt_refresh_token)


async def get_tt_session() -> Session | None:
    global _session
    if not tt_configured():
        return None
    if _session is not None:
        return _session
    async with _lock:
        if _session is None:
            s = get_settings()
            _session = Session(provider_secret=s.tt_provider_secret,
                               refresh_token=s.tt_refresh_token)
        return _session


def reset_tt_session() -> None:
    """Drop the cached session (call after credentials change)."""
    global _session
    _session = None
