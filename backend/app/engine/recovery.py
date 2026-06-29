"""Deterministic recovery sequence run on every transition into CONNECTED."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import TradeRecord
from app.engine.reconciliation import fetch_snapshot, reconcile
from app.engine.safe_mode import trip_safe_mode
from app.ibkr.gateway import ConnState
from app.services.journal import log_event

logger = logging.getLogger("trading.recovery")


async def local_open_positions() -> dict[str, int]:
    out: dict[str, int] = {}
    async with session_scope() as s:
        rows = (await s.execute(
            select(TradeRecord).where(TradeRecord.status == "open"))).scalars().all()
        for r in rows:
            signed = int(r.quantity) * (1 if (r.side or "").lower() in ("buy", "long") else -1)
            out[r.symbol] = out.get(r.symbol, 0) + signed
    return {k: v for k, v in out.items() if v != 0}


async def run_recovery(ib, gateway) -> bool:
    gateway.set_state(ConnState.RECOVERING)
    snap = await fetch_snapshot(ib)
    result = reconcile(snap["positions"], await local_open_positions())
    if not result.ok:
        reason = "; ".join(result.discrepancies)
        await trip_safe_mode(reason)
        gateway.set_state(ConnState.SAFE_MODE)
        await log_event(f"SAFE MODE on recovery: {reason}", level="error",
                        category="system")
        return False
    gateway.set_state(ConnState.READY)
    await log_event("Recovery clean — READY", category="system")
    return True
