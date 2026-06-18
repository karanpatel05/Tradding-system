"""Aggregate intraday bars into clean Regular-Trading-Hours (RTH) daily bars.

No Massive daily cache exists, and raw intraday bars include pre/post-market prints
(04:00, 18:00, ...). For daily strategies we want the *session* OHLC: open = the
09:30 print, close = the 16:00 print, high/low over RTH only. This drops extended
hours so 'overnight' (close->next open) means the real RTH gap.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)


def et_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_ET).date().isoformat()


def et_time(ms: int) -> time:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_ET).time()


def to_rth_daily(bars: list[dict]) -> list[dict]:
    """Intraday bars -> sorted list of RTH daily bars ``{date, open, high, low, close}``.

    Keeps only bars with 09:30 <= ET time < 16:00; the day's open is the first such
    bar's open and the close is the last such bar's close."""
    by_day: dict[str, list[dict]] = {}
    for b in sorted(bars, key=lambda x: x["time"]):
        t = et_time(b["time"])
        if _OPEN <= t < _CLOSE:
            by_day.setdefault(et_date(b["time"]), []).append(b)
    out = []
    for date in sorted(by_day):
        day = by_day[date]
        out.append({
            "date": date,
            "open": day[0]["open"],
            "high": max(b["high"] for b in day),
            "low": min(b["low"] for b in day),
            "close": day[-1]["close"],
        })
    return out
