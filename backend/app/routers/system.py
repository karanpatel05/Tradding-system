"""System ops endpoints: resume from SAFE_MODE (clear flag + re-run recovery),
live-orders master switch (ENABLE_LIVE_ORDERS, persisted to backend/.env)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.gate import require_auth
from app.auth.routes import _ENV_PATH, _upsert_env
from app.broker.status import is_ready_cached, probe
from app.config import get_settings
from app.engine.recovery import run_recovery
from app.engine.safe_mode import clear_safe_mode, is_safe_mode, trip_safe_mode

logger = logging.getLogger("trading.system")

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/resume", dependencies=[Depends(require_auth)])
async def resume() -> dict:
    await clear_safe_mode()
    try:
        await run_recovery()
    except Exception as exc:  # noqa: BLE001 — never leave safe mode cleared on a crash
        logger.error("recovery failed on resume", exc_info=True)
        await trip_safe_mode(f"recovery failed on resume: {type(exc).__name__}")
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


class LiveOrdersBody(BaseModel):
    enabled: bool


@router.put("/live-orders", dependencies=[Depends(require_auth)])
async def set_live_orders(body: LiveOrdersBody) -> dict:
    """Flip the ENABLE_LIVE_ORDERS master gate at runtime.

    Persist to backend/.env FIRST: if the write fails, the in-memory gate must
    not disagree with what survives a restart. The type-LIVE confirmation is
    frontend-only; this endpoint takes a plain boolean (auth-gated).

    Refused (409) while any trade is open, in either direction: a trade opened simulated
    was never sent to Alpaca (flipping ON would send real closes for it), and one sent to
    Alpaca would be "closed" by a simulated fill after flipping OFF."""
    s = get_settings()
    old = s.enable_live_orders
    if old != body.enabled:
        n_open = await _open_trade_count()
        if n_open:
            raise HTTPException(
                status_code=409,
                detail=f"{n_open} open trade(s): close or flatten them before switching "
                       f"order routing ({'Alpaca paper' if old else 'Simulated'} -> "
                       f"{'Alpaca paper' if body.enabled else 'Simulated'}).")
    _upsert_env(_ENV_PATH, {"ENABLE_LIVE_ORDERS": "true" if body.enabled else "false"})
    s.enable_live_orders = body.enabled
    if old != body.enabled:
        logger.warning(
            "[LIVE-ORDERS] switched %s -> %s (%s)",
            "ON" if old else "OFF", "ON" if body.enabled else "OFF",
            "REAL ORDERS ARMED" if body.enabled else "paper mode",
        )
    return {"live_orders_enabled": s.enable_live_orders}


async def _open_trade_count() -> int:
    from sqlalchemy import func, select

    from app.db.base import session_scope
    from app.db.models import TradeRecord
    async with session_scope() as s:
        return int((await s.execute(select(func.count()).select_from(TradeRecord).where(
            TradeRecord.status == "open"))).scalar_one())
