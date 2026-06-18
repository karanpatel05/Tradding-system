"""Auth = IBKR Gateway connection state (no OAuth, no per-user secrets).

Kept as a ``session_manager`` singleton so existing callers (kill switch, gate,
routers) keep working unchanged; ``is_authenticated()`` now means "Gateway
connected." There are no secrets to store — the Gateway login happens in IBKR's
own software, and the API connects to it locally."""
from __future__ import annotations

from app.ibkr.gateway import get_gateway


class SessionManager:
    """Process-wide auth state, backed by the IBKR Gateway connection."""

    def is_authenticated(self) -> bool:
        return get_gateway().is_connected()

    async def connect(self) -> bool:
        await get_gateway().connect()
        return self.is_authenticated()

    def disconnect(self) -> None:
        get_gateway().disconnect()


session_manager = SessionManager()
