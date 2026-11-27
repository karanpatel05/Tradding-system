"""US equity session calendar for the SPY and XSP iron flies.

Fails CLOSED: a year that is not configured here is not a trading day, so the strategy
skips rather than guessing. Historical holidays and early closes follow the NYSE
Group calendar, including the January 9, 2025 national day of mourning.
The 0DTE contract's own ``stops_trading_at`` is cross-checked at entry and
the earlier of the two closes wins.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

HOLIDAYS = {
    2024: {date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
           date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
           date(2024, 11, 28), date(2024, 12, 25)},
    2025: {date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
           date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
           date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25)},
    2026: {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
           date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
           date(2026, 11, 26), date(2026, 12, 25)},
    2027: {date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
           date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
           date(2027, 11, 25), date(2027, 12, 24)},
}
EARLY_CLOSES = {
    2024: {date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24)},
    2025: {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)},
    2026: {date(2026, 11, 27), date(2026, 12, 24)},
    2027: {date(2027, 11, 26)},
}


def calendar_configured(d: date) -> bool:
    return d.year in HOLIDAYS


def is_trading_day(d: date) -> bool:
    if not calendar_configured(d):
        return False
    return d.weekday() < 5 and d not in HOLIDAYS[d.year]


def is_early_close(d: date) -> bool:
    return d in EARLY_CLOSES.get(d.year, set())


def close_time(d: date) -> datetime | None:
    """The session close (ET, aware) or None when not a trading day."""
    if not is_trading_day(d):
        return None
    t = EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE
    return datetime.combine(d, t, tzinfo=ET)


def skip_reason(d: date) -> str | None:
    if not calendar_configured(d):
        return f"exchange calendar not configured for {d.year} (fail closed)"
    if d.weekday() >= 5:
        return "weekend"
    if d in HOLIDAYS[d.year]:
        return "exchange holiday"
    return None
