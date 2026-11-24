"""Engine-wide SAFE_MODE flag (persisted). When on, every order that OPENS or adds
exposure is blocked until a human clears it. Closing orders still go out: the kill
switch's flatten, and single-leg option closes (``orders.submit_option_order``, capped
at the app's own quantity) so a stop never leaves a short unprotected. Independent of
ENABLE_LIVE_ORDERS."""
from __future__ import annotations

import logging

from app.db.base import session_scope
from app.db.models import SystemFlag

logger = logging.getLogger("trading.safe_mode")
_KEY = "safe_mode"


async def trip_safe_mode(reason: str) -> None:
    async with session_scope() as s:
        row = await s.get(SystemFlag, _KEY)
        if row is None:
            s.add(SystemFlag(key=_KEY, on=True, reason=reason))
        else:
            row.on, row.reason = True, reason
    logger.error("SAFE MODE TRIPPED: %s", reason)


async def clear_safe_mode() -> None:
    async with session_scope() as s:
        row = await s.get(SystemFlag, _KEY)
        if row is not None:
            row.on, row.reason = False, ""
    logger.warning("safe mode cleared")


async def is_safe_mode() -> bool:
    async with session_scope() as s:
        row = await s.get(SystemFlag, _KEY)
        return bool(row and row.on)
