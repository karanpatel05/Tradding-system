"""Journal / calendar / logs routes (all gated)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.journal import (
    get_calendar,
    get_day_detail,
    get_logs,
    set_day_note,
)

# Read-only journal/calendar/logs are open so the Journal tab renders without a
# broker connection (the app no longer gates the whole UI behind login).
router = APIRouter(prefix="/api/journal", tags=["journal"])


@router.get("/calendar")
async def calendar(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict:
    """Day-by-day P&L. Defaults to the trailing ~3 months when no range given."""
    today = datetime.utcnow().date()
    start = start or (today - timedelta(days=92))
    end = end or today
    return {"start": start.isoformat(), "end": end.isoformat(), "days": await get_calendar(start, end)}


@router.get("/day/{day}")
async def day_detail(day: date) -> dict:
    return await get_day_detail(day)


class NoteBody(BaseModel):
    text: str


@router.put("/day/{day}/note")
async def upsert_note(day: date, body: NoteBody) -> dict:
    await set_day_note(day, body.text)
    return {"date": day.isoformat(), "text": body.text}


logs_router = APIRouter(prefix="/api", tags=["logs"])


@logs_router.get("/logs")
async def logs(
    limit: int = Query(default=200, le=1000),
    category: str | None = None,
    strategy_key: str | None = None,
) -> dict:
    return {"logs": await get_logs(limit=limit, category=category, strategy_key=strategy_key)}
