"""Deterministic recovery: reconcile broker truth vs local records at startup (and on
demand). Mismatch -> SAFE_MODE until a human clears it; clean -> READY + backfill."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import TradeRecord
from app.engine.reconciliation import fetch_snapshot, reconcile
from app.engine.safe_mode import trip_safe_mode
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


async def run_recovery() -> bool:
    snap = await fetch_snapshot()
    result = reconcile(snap["positions"], await local_open_positions())
    if not result.ok:
        reason = "; ".join(result.discrepancies)
        await trip_safe_mode(reason)
        await log_event(f"SAFE MODE on recovery: {reason}", level="error", category="system")
        from app.services.alerts import send_alert
        await send_alert("safe_mode", reason, {"discrepancies": result.discrepancies})
        return False
    _schedule_backfill()
    await log_event("Recovery clean — READY", category="system")
    return True


def _schedule_backfill() -> None:
    import asyncio

    from app.services.backfill import backfill_active
    asyncio.create_task(backfill_active())
