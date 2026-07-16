"""Logging + journal/calendar services.

``log_event`` is the single writer used everywhere (engine decisions, trades,
auth/system events). Calendar P&L is aggregated from closed ``TradeRecord`` rows
grouped by the date of their ``exit_time`` — computed in Python so it behaves
identically on SQLite and Postgres.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import (BuilderPositionRecord, JournalNote, LogEntry,
                           OptionPositionRecord, TradeRecord)

logger = logging.getLogger("trading.journal")

VALID_CATEGORIES = {"action", "signal", "trade", "decision", "system"}


async def log_event(
    message: str,
    *,
    category: str = "action",
    level: str = "info",
    strategy_key: str | None = None,
    symbol: str | None = None,
    data: dict | None = None,
) -> None:
    """Persist a structured log entry. Never raises into the caller — logging a
    decision must not break the decision."""
    if category not in VALID_CATEGORIES:
        category = "action"
    try:
        async with session_scope() as session:
            session.add(LogEntry(
                level=level, category=category, strategy_key=strategy_key,
                symbol=symbol, message=message, data=data,
            ))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist log event: %s", message)


async def get_logs(limit: int = 200, category: str | None = None,
                   strategy_key: str | None = None) -> list[dict]:
    async with session_scope() as session:
        stmt = select(LogEntry).order_by(LogEntry.ts.desc()).limit(limit)
        if category:
            stmt = stmt.where(LogEntry.category == category)
        if strategy_key:
            stmt = stmt.where(LogEntry.strategy_key == strategy_key)
        rows = (await session.execute(stmt)).scalars().all()
    return [_log_to_dict(r) for r in rows]




def _tc_dollars(rec) -> float:
    """TC option pnl is stored per-share; dollars = pnl x 100 x contracts."""
    return round((rec.pnl or 0.0) * 100 * (rec.contracts or 1), 2)


async def _option_rows(session) -> list[dict]:
    """TC + Builder option positions, normalized for calendar aggregation.
    Builder pnl is already dollars; TC converts per-share -> dollars."""
    tc = (await session.execute(select(OptionPositionRecord))).scalars().all()
    br = (await session.execute(select(BuilderPositionRecord))).scalars().all()
    rows: list[dict] = []
    for r in tc:
        rows.append({"source": "triple_calendar", "underlying": r.underlying,
                     "status": r.status, "entry_time": r.entry_time,
                     "exit_time": r.exit_time, "contracts": r.contracts or 1,
                     "pnl": _tc_dollars(r) if r.status == "closed" else None})
    for r in br:
        rows.append({"source": "builder", "underlying": r.underlying,
                     "status": r.status, "entry_time": r.entry_time,
                     "exit_time": r.exit_time, "contracts": r.contracts or 1,
                     "pnl": (round(r.pnl, 2) if r.pnl is not None else 0.0)
                            if r.status == "closed" else None})
    return rows

async def get_calendar(start: date, end: date) -> list[dict]:
    """Day-by-day summary between start and end (inclusive).

    Closed trades are bucketed by their ``exit_time`` (realized P&L + win stats);
    still-open trades are bucketed by their ``entry_time`` so a trade shows up on
    the calendar the day it is *taken*, before it closes. Realized P&L and win rate
    are computed from closed trades only."""
    async with session_scope() as session:
        trades = (await session.execute(select(TradeRecord))).scalars().all()
        options = await _option_rows(session)

    closed_by_day: dict[date, list[TradeRecord]] = defaultdict(list)
    open_by_day: dict[date, int] = defaultdict(int)
    # Options (TC rolls/exits + Builder paper closes) count like trades: closed by
    # exit day (realized dollars), open by entry day.
    opt_closed_by_day: dict[date, list[dict]] = defaultdict(list)
    for o in options:
        if o["status"] == "closed" and o["exit_time"]:
            d = o["exit_time"].date()
            if start <= d <= end:
                opt_closed_by_day[d].append(o)
        elif o["status"] == "open" and o["entry_time"]:
            d = o["entry_time"].date()
            if start <= d <= end:
                open_by_day[d] += 1
    for t in trades:
        if t.status == "closed" and t.exit_time:
            d = t.exit_time.date()
            if start <= d <= end:
                closed_by_day[d].append(t)
        elif t.status == "open" and t.entry_time:
            d = t.entry_time.date()
            if start <= d <= end:
                open_by_day[d] += 1

    days = []
    for d in sorted(set(closed_by_day) | set(open_by_day) | set(opt_closed_by_day)):
        day_trades = closed_by_day.get(d, [])
        day_opts = opt_closed_by_day.get(d, [])
        open_n = open_by_day.get(d, 0)
        pnl = sum(t.pnl or 0.0 for t in day_trades) + sum(o["pnl"] or 0.0 for o in day_opts)
        wins = sum(1 for t in day_trades if (t.pnl or 0.0) > 0) \
             + sum(1 for o in day_opts if (o["pnl"] or 0.0) > 0)
        closed_n = len(day_trades) + len(day_opts)
        days.append({
            "date": d.isoformat(),
            "realized_pnl": round(pnl, 2),
            "trade_count": closed_n + open_n,
            "closed_count": closed_n,
            "open_count": open_n,
            "win_count": wins,
            "win_rate": round(wins / closed_n, 4) if closed_n else 0.0,
        })
    return days


async def get_day_detail(day: date) -> dict:
    """Every trade, log, and the note for a single calendar day.

    Includes closed trades (by ``exit_time``) and open trades taken that day (by
    ``entry_time``) so a live trade appears as soon as it's entered. Realized P&L
    sums closed trades only."""
    async with session_scope() as session:
        trades = (await session.execute(select(TradeRecord))).scalars().all()
        options = await _option_rows(session)
        logs = (await session.execute(
            select(LogEntry).order_by(LogEntry.ts.asc())
        )).scalars().all()
        note = await session.get(JournalNote, day)
        note_text = note.text if note else ""

    day_trades = [
        t for t in trades
        if (t.status == "closed" and t.exit_time and t.exit_time.date() == day)
        or (t.status == "open" and t.entry_time and t.entry_time.date() == day)
    ]
    day_trades.sort(key=lambda t: (t.entry_time or t.exit_time or datetime.min))
    day_opts = [
        o for o in options
        if (o["status"] == "closed" and o["exit_time"] and o["exit_time"].date() == day)
        or (o["status"] == "open" and o["entry_time"] and o["entry_time"].date() == day)
    ]
    day_opts.sort(key=lambda o: (o["entry_time"] or o["exit_time"] or datetime.min))
    day_logs = [r for r in logs if r.ts.date() == day]
    realized = sum(t.pnl or 0.0 for t in day_trades if t.status == "closed") \
             + sum(o["pnl"] or 0.0 for o in day_opts if o["status"] == "closed")

    return {
        "date": day.isoformat(),
        "realized_pnl": round(realized, 2),
        "trades": [_trade_to_dict(t) for t in day_trades],
        "option_trades": [
            {"source": o["source"], "underlying": o["underlying"], "status": o["status"],
             "contracts": o["contracts"], "pnl": o["pnl"],
             "entry_time": o["entry_time"].isoformat() if o["entry_time"] else None,
             "exit_time": o["exit_time"].isoformat() if o["exit_time"] else None}
            for o in day_opts
        ],
        "logs": [_log_to_dict(r) for r in day_logs],
        "note": note_text,
    }


async def set_day_note(day: date, text: str) -> None:
    async with session_scope() as session:
        note = await session.get(JournalNote, day)
        if note:
            note.text = text
        else:
            session.add(JournalNote(day=day, text=text))


def _trade_to_dict(t: TradeRecord) -> dict:
    return {
        "id": t.id,
        "strategy_key": t.strategy_key,
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "pnl": t.pnl,
        "return_pct": t.return_pct,
        "status": t.status,
    }


def _log_to_dict(r: LogEntry) -> dict:
    return {
        "id": r.id,
        "ts": r.ts.isoformat() if r.ts else None,
        "level": r.level,
        "category": r.category,
        "strategy_key": r.strategy_key,
        "symbol": r.symbol,
        "message": r.message,
        "data": r.data,
    }
