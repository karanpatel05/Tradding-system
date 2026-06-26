"""Persist/reconstruct the live Triple Calendar position across restarts."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import OptionPositionRecord
from app.strategies.triple_calendar.legs import Calendar, Position


def _ser(pos: Position) -> list[dict]:
    return [{"strike": c.strike, "sell": c.sell_expiry.isoformat(), "buy": c.buy_expiry.isoformat()}
            for c in pos.calendars]


def _deser(underlying: str, entry: datetime, debit: float, cals: list[dict]) -> Position:
    return Position(underlying=underlying, entry_date=entry.date(), net_debit=debit, calendars=[
        Calendar(c["strike"], date.fromisoformat(c["sell"]), date.fromisoformat(c["buy"]))
        for c in cals])


async def save_position(strategy_key: str, pos: Position, order_ids: list[int], paper: bool) -> int:
    async with session_scope() as session:
        rec = OptionPositionRecord(
            strategy_key=strategy_key, underlying=pos.underlying, status="open", paper=paper,
            net_debit=pos.net_debit, calendars=_ser(pos), order_ids=list(order_ids))
        session.add(rec)
        await session.flush()
        return rec.id


async def load_open(strategy_key: str) -> Position | None:
    async with session_scope() as session:
        rec = (await session.execute(
            select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == strategy_key,
                OptionPositionRecord.status == "open"))).scalars().first()
        if rec is None:
            return None
        return _deser(rec.underlying, rec.entry_time, rec.net_debit, rec.calendars)


async def update_calendars(record_id: int, pos: Position) -> None:
    async with session_scope() as session:
        rec = await session.get(OptionPositionRecord, record_id)
        if rec:
            rec.calendars = _ser(pos)
            rec.net_debit = pos.net_debit


async def close_position(record_id: int, pnl: float) -> None:
    async with session_scope() as session:
        rec = await session.get(OptionPositionRecord, record_id)
        if rec:
            rec.status = "closed"
            rec.pnl = pnl
            rec.exit_time = datetime.now(timezone.utc)
