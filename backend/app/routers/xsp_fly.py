"""XSP iron fly: read-only session view, and clearing the persisted daily-loss halt."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.gate import require_auth
from app.services.journal import log_event
from app.strategies.registry import get_strategy

router = APIRouter(prefix="/api/strategies/xsp_iron_fly", tags=["strategies"])
KEY = "xsp_iron_fly"


@router.get("/sessions")
async def sessions() -> dict:
    """Recent trading days: status, legs, stop levels, fills count, P&L, reconciliation."""
    return await get_strategy(KEY).status_view()


@router.post("/clear-halt", dependencies=[Depends(require_auth)])
async def clear_halt() -> dict:
    """A human clears the max_daily_loss halt before entries resume."""
    from app.strategies.xsp_iron_fly import store
    was = await store.clear_halt()
    if was:
        await log_event("XSP fly daily-loss halt cleared by operator", category="action",
                        strategy_key=KEY)
    return {"cleared": was}
