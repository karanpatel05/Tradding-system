"""System ops endpoints: resume from SAFE_MODE (clear flag + re-run recovery)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.gate import require_auth
from app.engine.recovery import run_recovery
from app.engine.safe_mode import clear_safe_mode, is_safe_mode
from app.ibkr.gateway import get_gateway

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/resume", dependencies=[Depends(require_auth)])
async def resume() -> dict:
    gw = get_gateway()
    if gw._ib is None:
        return {"conn_state": gw.state.value, "safe_mode": await is_safe_mode(),
                "detail": "not connected"}
    await clear_safe_mode()
    await run_recovery()
    from app.db.base import session_scope
    from app.db.models import SystemFlag
    reason = None
    async with session_scope() as s:
        row = await s.get(SystemFlag, "safe_mode")
        if row and row.on:
            reason = row.reason or None
    return {"conn_state": gw.state.value, "safe_mode": await is_safe_mode(),
            "safe_mode_reason": reason}
