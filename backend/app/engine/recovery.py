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


async def _await_account_ready(ib) -> None:
    """Wait for a (re)connected Gateway's account data to re-sync before reconciling, so a
    nightly-restart's brief empty-positions window doesn't look like a real mismatch."""
    import asyncio

    from app.config import get_settings
    waited = 0.0
    while waited < 10.0:
        try:
            # A stub IB without managedAccounts is treated as "ready" (don't wait 10s in tests).
            accts = ib.managedAccounts() if hasattr(ib, "managedAccounts") else ["_"]
            if accts:
                break
        except Exception:  # noqa: BLE001 — not ready yet
            pass
        await asyncio.sleep(0.2)
        waited += 0.2
    settle = get_settings().ibkr_account_settle_seconds
    if settle:
        await asyncio.sleep(settle)


async def run_recovery(ib, gateway) -> bool:
    gateway.set_state(ConnState.RECOVERING)
    await _await_account_ready(ib)
    snap = await fetch_snapshot(ib)
    result = reconcile(snap["positions"], await local_open_positions())
    if not result.ok:
        reason = "; ".join(result.discrepancies)
        await trip_safe_mode(reason)
        gateway.set_state(ConnState.SAFE_MODE)
        await log_event(f"SAFE MODE on recovery: {reason}", level="error",
                        category="system")
        from app.services.alerts import send_alert
        await send_alert("safe_mode", reason, {"discrepancies": result.discrepancies})
        return False
    gateway.set_state(ConnState.READY)
    _schedule_backfill()
    await log_event("Recovery clean — READY", category="system")
    return True


def _schedule_backfill() -> None:
    import asyncio

    from app.services.backfill import backfill_active
    asyncio.create_task(backfill_active())
