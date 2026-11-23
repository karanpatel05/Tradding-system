"""Persistence for the in-app QQQ spread: the per-day session row and the two leg
TradeRecords (transmitted=True: they are real Alpaca-paper fills, so startup
reconciliation compares them with the app-owned Alpaca ledger)."""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import session_scope
from app.db.models import QqqSpreadSession, TradeRecord


def _aware(dt):
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_dict(row: QqqSpreadSession) -> dict:
    return {"id": row.id, "strategy_key": row.strategy_key, "session_date": row.session_date,
            "status": row.status, "reason": row.reason or "",
            "data": copy.deepcopy(row.data or {}), "realized_pnl": row.realized_pnl,
            "updated_at": _aware(row.updated_at)}


async def get_session(key: str, d: date) -> dict | None:
    async with session_scope() as s:
        row = (await s.execute(select(QqqSpreadSession).where(
            QqqSpreadSession.strategy_key == key,
            QqqSpreadSession.session_date == d))).scalars().first()
        return _to_dict(row) if row else None


async def create_session(key: str, d: date, status: str = "pending", reason: str = "",
                         data: dict | None = None) -> dict | None:
    """Insert the day's row; None if it already exists (the idempotency guard)."""
    try:
        async with session_scope() as s:
            row = QqqSpreadSession(strategy_key=key, session_date=d, status=status,
                                   reason=reason, data=copy.deepcopy(data or {}))
            s.add(row)
            await s.flush()
            return _to_dict(row)
    except IntegrityError:
        return None


async def save(sess: dict) -> None:
    async with session_scope() as s:
        row = await s.get(QqqSpreadSession, sess["id"])
        row.status = sess["status"]
        row.reason = sess.get("reason") or ""
        row.data = copy.deepcopy(sess.get("data") or {})
        row.realized_pnl = sess.get("realized_pnl")


async def recent_sessions(key: str, limit: int = 10) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(select(QqqSpreadSession).where(
            QqqSpreadSession.strategy_key == key).order_by(
            QqqSpreadSession.session_date.desc()).limit(limit))).scalars().all()
        return [_to_dict(r) for r in rows]


async def unresolved_before(key: str, d: date) -> list[dict]:
    """Earlier days still marked as holding (or entering) a position."""
    async with session_scope() as s:
        rows = (await s.execute(select(QqqSpreadSession).where(
            QqqSpreadSession.strategy_key == key, QqqSpreadSession.session_date < d,
            QqqSpreadSession.status.in_(("entering", "open", "closing"))))).scalars().all()
        return [_to_dict(r) for r in rows]


async def open_leg_trades(key: str, short_occ: str, long_occ: str, qty: int,
                          short_px: float, long_px: float, now: datetime) -> dict[str, int]:
    """Two open TradeRecords (short leg sold, long leg bought). Returns their ids."""
    ids = {}
    async with session_scope() as s:
        for sym, side, px in ((short_occ, "sell", short_px), (long_occ, "buy", long_px)):
            tr = TradeRecord(strategy_key=key, symbol=sym, side=side, quantity=float(qty),
                             entry_price=round(px, 4), entry_time=now.astimezone(timezone.utc),
                             status="open", transmitted=True)
            s.add(tr)
            await s.flush()
            ids[side] = tr.id
    return ids


async def close_leg_trades(trade_ids: dict, exit_prices: dict[str, float], now: datetime,
                           remaining: dict[str, int] | None = None) -> float:
    """Close (or shrink, when ``remaining`` leaves some open) the leg TradeRecords at
    these per-leg exit prices. Returns the realized $ P&L booked by this call."""
    booked = 0.0
    async with session_scope() as s:
        for side, tid in (trade_ids or {}).items():
            tr = await s.get(TradeRecord, tid)
            if tr is None or tr.status != "open":
                continue
            px = exit_prices.get(side)
            if px is None:
                continue
            left = int((remaining or {}).get(side, 0))
            closed_qty = int(tr.quantity or 0) - left
            if closed_qty <= 0:
                continue
            sign = 1 if side == "sell" else -1     # short leg profits when exit < entry
            pnl = round(sign * ((tr.entry_price or 0) - px) * closed_qty * 100, 2)
            booked += pnl
            if left > 0:
                tr.quantity = float(left)
                tr.pnl = round((tr.pnl or 0) + pnl, 2)
                continue
            tr.status = "closed"
            tr.exit_price = round(px, 4)
            tr.exit_time = now.astimezone(timezone.utc)
            tr.pnl = round((tr.pnl or 0) + pnl, 2)
            cost = (tr.entry_price or 0) * closed_qty * 100
            tr.return_pct = round(pnl / cost, 6) if cost else None
    return round(booked, 2)


async def close_leg_trades_unpriced(trade_ids: dict, now: datetime) -> None:
    """The broker shows the legs flat but no fill price is known (closed outside the
    strategy): close the records so reconciliation agrees; P&L stays unknown."""
    async with session_scope() as s:
        for tid in (trade_ids or {}).values():
            tr = await s.get(TradeRecord, tid)
            if tr is not None and tr.status == "open":
                tr.status = "closed"
                tr.exit_time = now.astimezone(timezone.utc)
