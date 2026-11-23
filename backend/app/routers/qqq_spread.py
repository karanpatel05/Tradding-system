"""QQQ credit spread: read-only view of the in-app sessions and the external daemon."""
from __future__ import annotations

from fastapi import APIRouter

from app.strategies.registry import get_strategy

router = APIRouter(prefix="/api/strategies/qqq_credit_spread", tags=["strategies"])
KEY = "qqq_credit_spread"


@router.get("/sessions")
async def sessions() -> dict:
    """Recent days: status, spread, fills, checkpoint decision, P&L; daemon state."""
    return await get_strategy(KEY).status_view()
