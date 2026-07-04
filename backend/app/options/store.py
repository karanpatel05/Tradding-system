"""Persistence for builder (tracked paper) trades. No order side effects — pure DB."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import BuilderPositionRecord


async def save_trade(underlying: str, legs: list[dict], net_debit: float, iv: float,
                     target_pct: float | None, stop_pct: float | None) -> int:
    async with session_scope() as session:
        rec = BuilderPositionRecord(
            underlying=underlying.upper(), legs=list(legs), net_debit=net_debit, iv=iv,
            target_pct=target_pct, stop_pct=stop_pct, status="open")
        session.add(rec)
        await session.flush()
        return rec.id


async def list_trades(status: str | None = None) -> list[BuilderPositionRecord]:
    async with session_scope() as session:
        stmt = select(BuilderPositionRecord)
        if status:
            stmt = stmt.where(BuilderPositionRecord.status == status)
        # open first, then newest by entry.
        stmt = stmt.order_by(BuilderPositionRecord.status.desc(),
                             BuilderPositionRecord.entry_time.desc())
        return list((await session.execute(stmt)).scalars().all())


async def list_open() -> list[BuilderPositionRecord]:
    return await list_trades(status="open")


async def get_trade(trade_id: int) -> BuilderPositionRecord | None:
    async with session_scope() as session:
        return await session.get(BuilderPositionRecord, trade_id)


async def update_valuation(trade_id: int, value: float, pnl: float, pnl_pct: float) -> None:
    async with session_scope() as session:
        rec = await session.get(BuilderPositionRecord, trade_id)
        if rec and rec.status == "open":
            rec.last_value = value
            rec.last_pnl = pnl
            rec.last_pnl_pct = pnl_pct
            rec.valued_at = datetime.now(timezone.utc)


async def close_trade(trade_id: int, pnl: float | None, reason: str) -> None:
    async with session_scope() as session:
        rec = await session.get(BuilderPositionRecord, trade_id)
        if rec and rec.status == "open":
            rec.status = "closed"
            rec.pnl = pnl
            rec.exit_reason = reason
            rec.exit_time = datetime.now(timezone.utc)
