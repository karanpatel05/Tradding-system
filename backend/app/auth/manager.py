"""Auth = Tastytrade OAuth pair readiness (probe-backed, cached).

Kept as a ``session_manager`` singleton so existing callers (kill switch, gate,
routers) keep working unchanged. ``is_authenticated()`` reads the cached probe
result (see ``app.broker.status``); ``connect()`` forces a fresh probe;
``disconnect()`` is a no-op (kept for API compat — there is no persistent broker
session to tear down, readiness is simply re-derived by the next probe)."""
from __future__ import annotations

from app.broker.status import is_ready_cached, probe


class SessionManager:
    """Process-wide auth state, backed by the Tastytrade OAuth probe."""

    def is_authenticated(self) -> bool:
        return is_ready_cached()

    async def connect(self) -> bool:
        await probe(force=True)
        return is_ready_cached()

    def disconnect(self) -> None:
        # No-op: kept for API compat. There is no persistent session to tear down;
        # the next probe (cached or forced) re-derives readiness independently.
        pass


session_manager = SessionManager()
