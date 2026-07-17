"""System ops endpoints: resume from SAFE_MODE (clear flag + re-run recovery)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.gate import require_auth
from app.broker.status import is_ready_cached, probe
from app.engine.recovery import run_recovery
from app.engine.safe_mode import clear_safe_mode, is_safe_mode

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/resume", dependencies=[Depends(require_auth)])
async def resume() -> dict:
    await clear_safe_mode()
    await run_recovery()
    await probe(force=True)
    from app.db.base import session_scope
    from app.db.models import SystemFlag
    sm_on = await is_safe_mode()
    reason = None
    if sm_on:
        async with session_scope() as s:
            row = await s.get(SystemFlag, "safe_mode")
            if row and row.on:
                reason = row.reason or None
    conn_state = "safe_mode" if sm_on else ("ready" if is_ready_cached() else "disconnected")
    return {"conn_state": conn_state, "safe_mode": sm_on, "safe_mode_reason": reason}
