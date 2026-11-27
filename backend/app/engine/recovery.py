"""Deterministic recovery: reconcile broker truth vs local records at startup (and on
demand). Mismatch -> SAFE_MODE until a human clears it; clean -> READY + backfill."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.broker.alpaca_client import occ_unpadded
from app.db.base import session_scope
from app.db.models import TradeRecord
from app.engine.reconciliation import BrokerUnavailable, fetch_snapshot, reconcile
from app.engine.safe_mode import trip_safe_mode
from app.services.journal import log_event

logger = logging.getLogger("trading.recovery")


async def local_open_positions() -> dict[str, int]:
    """Open trades actually transmitted to Alpaca (simulated ones never reach the broker),
    keyed by unpadded OCC symbol to match the ledger. Options past expiry are dropped,
    exactly as the ledger drops them (a cash-settled leg has no closing order; its
    TradeRecord closes when the strategy books the settlement)."""
    from app.broker.ledger import option_expired
    from app.strategies.xsp_iron_fly import store as xsp_store
    fly_keys = ("xsp_iron_fly", "spy_iron_fly")
    out: dict[str, int] = {}
    async with session_scope() as s:
        rows = (await s.execute(select(TradeRecord).where(
            TradeRecord.status == "open", TradeRecord.transmitted.is_(True)))).scalars().all()
        for r in rows:
            if r.strategy_key in fly_keys:   # counted from its sessions below
                continue
            signed = int(r.quantity) * (1 if (r.side or "").lower() in ("buy", "long") else -1)
            sym = occ_unpadded(r.symbol)
            if option_expired(sym):
                continue
            out[sym] = out.get(sym, 0) + signed
    # Use actual fly session fills for both underlyings. Their leg
    # TradeRecords only exist after the entry completes and keep the entry qty through
    # a partial stop, so they would false-trip SAFE_MODE mid-entry or mid-stop.
    for key in fly_keys:
        for sym, q in (await xsp_store.broker_expected_positions(key)).items():
            if not option_expired(sym):
                out[sym] = out.get(sym, 0) + q
    return {k: v for k, v in out.items() if v != 0}


async def run_recovery() -> bool:
    try:
        snap = await fetch_snapshot()
    except BrokerUnavailable as exc:
        await _safe_mode("broker unavailable: Alpaca", [f"broker unavailable: {exc}"])
        return False
    discrepancies = (reconcile(snap["positions"], await local_open_positions()).discrepancies
                     + snap["issues"])
    if discrepancies:
        await _safe_mode("; ".join(discrepancies), discrepancies)
        return False
    _schedule_backfill()
    await log_event("Recovery clean — READY", category="system")
    return True


async def _safe_mode(reason: str, discrepancies: list[str]) -> None:
    await trip_safe_mode(reason)
    await log_event(f"SAFE MODE on recovery: {'; '.join(discrepancies)}", level="error",
                    category="system")
    from app.services.alerts import send_alert
    await send_alert("safe_mode", reason, {"discrepancies": discrepancies})


def _schedule_backfill() -> None:
    import asyncio

    from app.services.backfill import backfill_active
    asyncio.create_task(backfill_active())
