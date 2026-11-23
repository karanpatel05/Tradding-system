"""Pure aggregation of stored 1-minute bars into the chart's coarser candles.

Intraday buckets sit on New York clock boundaries, daily bars cover the regular
session only, weekly bars start on Monday (ET). No I/O.
"""
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}

_SESSION_OPEN = 9 * 60 + 30
_SESSION_CLOSE = 16 * 60


def _et(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, ET)


def _to_ms(day: date, minute_of_day: int) -> int:
    local = datetime.combine(day, dtime(minute_of_day // 60, minute_of_day % 60), tzinfo=ET)
    return int(local.timestamp() * 1000)


def et_session_start_ms(day: date) -> int:
    """09:30 New York time on `day`, as UTC epoch milliseconds."""
    return _to_ms(day, _SESSION_OPEN)


def _merge(rows: list[dict], time_ms: int) -> dict:
    return {
        "time": time_ms,
        "open": rows[0]["open"],
        "high": max(r["high"] for r in rows),
        "low": min(r["low"] for r in rows),
        "close": rows[-1]["close"],
        "volume": sum(r["volume"] for r in rows),
    }


def bucket(minutes: list[dict], interval: str) -> list[dict]:
    """Group 1m bars (oldest first) into ET-clock buckets of `interval`."""
    n = INTRADAY_MINUTES[interval]
    if n == 1:
        return minutes
    groups: dict[tuple[date, int], list[dict]] = {}
    for bar in minutes:
        local = _et(bar["time"])
        start = (local.hour * 60 + local.minute) // n * n
        groups.setdefault((local.date(), start), []).append(bar)
    return [_merge(rows, _to_ms(day, start)) for (day, start), rows in groups.items()]


def daily_from_minutes(minutes: list[dict]) -> list[dict]:
    """One bar per ET date from regular-session minutes [09:30, 16:00)."""
    groups: dict[date, list[dict]] = {}
    for bar in minutes:
        local = _et(bar["time"])
        if _SESSION_OPEN <= local.hour * 60 + local.minute < _SESSION_CLOSE:
            groups.setdefault(local.date(), []).append(bar)
    return [
        {**_merge(rows, et_session_start_ms(day)), "session": day.isoformat()}
        for day, rows in groups.items()
    ]


def weekly_from_daily(days: list[dict]) -> list[dict]:
    """Group daily rows by ET week (Monday start); time = first day's time."""
    groups: dict[date, list[dict]] = {}
    for row in days:
        d = _et(row["time"]).date()
        groups.setdefault(d - timedelta(days=d.weekday()), []).append(row)
    return [_merge(rows, rows[0]["time"]) for rows in groups.values()]
