"""Archive provider-reported equity minutes after the regular close, even without viewers."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.providers.archive_minutes import open_minute_source
from app.services import minute_store
from app.services.chart_data import normalize_symbol
from app.services.market import AFTERHOURS_CLOSE, ET, PREMARKET_OPEN, regular_session_close
from app.tastytrade.session import tt_configured

logger = logging.getLogger("trading.minute_archive")
DAY = timedelta(days=1)
REFRESH_SECONDS = 300
FINALIZE_AFTER = timedelta(minutes=20)


def _latest_closed_session(now: datetime) -> date:
    day = now.date()
    while True:
        close = regular_session_close(day)
        if close is not None and close <= now:
            return day
        day -= DAY


async def _pending_days(symbol: str, latest: date) -> list[date]:
    completed = await minute_store.saved_through(symbol)
    day = completed + DAY if completed else await minute_store.archive_start(symbol, latest)
    days = []
    # Make progress on every symbol when the backend returns from a long outage.
    while day <= latest and len(days) < 3:
        if regular_session_close(day) is not None:
            days.append(day)
        day += DAY
    return days


async def _save_session(source, symbol: str, day: date, now: datetime, *, advance: bool = True) -> None:
    start = datetime.combine(day, PREMARKET_OPEN, ET)
    end = datetime.combine(day, AFTERHOURS_CLOSE, ET)
    final = advance and now >= end + FINALIZE_AFTER
    if not final:
        # Only complete minutes. Re-read a small overlap for late revisions.
        end = min(end, now.replace(second=0, microsecond=0))
        last = await minute_store.latest_minute(symbol)
        if last is not None and start.timestamp() * 1000 <= last < end.timestamp() * 1000:
            overlap = datetime.fromtimestamp(last / 1000, ET) - timedelta(minutes=2)
            start = max(start, overlap)
    bars = await source.fetch(symbol, start, end)
    await minute_store.save_minutes(symbol, bars, completed_day=day if final else None)
    logger.info("Archived %d one-minute bars for %s on %s (%s)",
                len(bars), symbol, day, "final" if final else "updating")


async def archive_once(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    symbols = dict.fromkeys(normalize_symbol(s) for s in get_settings().market_archive_symbols.split(",")
                            if s.strip())
    latest = _latest_closed_session(now)
    pending = {}
    for symbol in symbols:
        days = await _pending_days(symbol, latest)
        if days:
            pending[symbol] = days
    if not pending:
        return True

    success = True
    async with open_minute_source() as source:
        for symbol, days in pending.items():
            # Protect the newest session even if older history is no longer
            # available. Only consecutive, fully reconciled days advance the cursor.
            work = [(day, True) for day in days]
            if days[0] < latest:
                work.insert(0, (latest, False))
            for day, advance in work:
                try:
                    await _save_session(source, symbol, day, now, advance=advance)
                except Exception:
                    # Leave the checkpoint before the failed day. Other symbols
                    # can still finish, including when one ticker is unavailable.
                    logger.warning("Minute archive will retry %s on %s", symbol, day, exc_info=True)
                    success = False
                    break
    return success


async def run_minute_archive() -> None:
    while True:
        try:
            if tt_configured() and get_settings().market_archive_symbols.strip():
                await archive_once()
        except Exception:
            logger.exception("Minute archive pass failed; will retry")
        await asyncio.sleep(REFRESH_SECONDS)
