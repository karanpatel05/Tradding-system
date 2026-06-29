"""Durable per-strategy worker state (managed symbol, armed entry, open trade)."""
from __future__ import annotations

from app.db.base import session_scope
from app.db.models import WorkerState

_SENTINEL = object()


async def save_state(strategy_key: str, *, managed_symbol=_SENTINEL, armed=_SENTINEL,
                     open_trade_id=_SENTINEL, last_bar_ms=_SENTINEL) -> None:
    async with session_scope() as s:
        row = await s.get(WorkerState, strategy_key)
        if row is None:
            row = WorkerState(strategy_key=strategy_key)
            s.add(row)
        if managed_symbol is not _SENTINEL:
            row.managed_symbol = managed_symbol
        if armed is not _SENTINEL:
            row.armed = armed
        if open_trade_id is not _SENTINEL:
            row.open_trade_id = open_trade_id
        if last_bar_ms is not _SENTINEL:
            row.last_bar_ms = last_bar_ms


async def load_state(strategy_key: str) -> dict | None:
    async with session_scope() as s:
        row = await s.get(WorkerState, strategy_key)
        if row is None:
            return None
        return {"managed_symbol": row.managed_symbol, "armed": row.armed,
                "open_trade_id": row.open_trade_id, "last_bar_ms": row.last_bar_ms}
