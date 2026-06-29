"""Durable order-intent store for idempotent submission + reconnect dedupe."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import OrderIntent

_OPEN = ("submitting", "working")


def new_ref(strategy_key: str, symbol: str) -> str:
    return f"{strategy_key}-{symbol}-{uuid4().hex[:8]}"


async def record_intent(strategy_key: str, symbol: str, intent: str, quantity: int) -> str:
    ref = new_ref(strategy_key, symbol)
    async with session_scope() as s:
        s.add(OrderIntent(client_order_ref=ref, strategy_key=strategy_key, symbol=symbol,
                          intent=intent, quantity=int(quantity), status="submitting"))
    return ref


async def mark_perm_id(ref: str, perm_id: str) -> None:
    async with session_scope() as s:
        row = (await s.execute(
            select(OrderIntent).where(OrderIntent.client_order_ref == ref))).scalars().first()
        if row is not None:
            row.perm_id = str(perm_id)


async def set_status(ref: str, status: str) -> None:
    async with session_scope() as s:
        row = (await s.execute(
            select(OrderIntent).where(OrderIntent.client_order_ref == ref))).scalars().first()
        if row is not None:
            row.status = status


async def open_refs() -> list[str]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(OrderIntent).where(OrderIntent.status.in_(_OPEN)))).scalars().all()
        return [r.client_order_ref for r in rows]
