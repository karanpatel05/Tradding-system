"""Regular-trading-hours (RTH) gating for the EMA crossover.

Mirrors the scalper's session approach: EMAs still compute over every bar (so
backtest and live see identical indicator inputs — no bar filtering, which would
break parity), but **entries** are confined to ``[session_start_et,
session_cutoff_et)`` and any open position is **force-flat** at/after
``session_close_et``. ``can_open`` / ``must_flat`` are exactly the flags the shared
``signals.decide_index`` already consumes, so wiring them in gates backtest and
live identically.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _et_time(ms: int) -> time:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_ET).time()


def can_open(ms: int, start_et: str, cutoff_et: str) -> bool:
    """True if the bar's ET time is within ``[start_et, cutoff_et)`` — entries allowed."""
    return _parse_hhmm(start_et) <= _et_time(ms) < _parse_hhmm(cutoff_et)


def must_flat(ms: int, close_et: str) -> bool:
    """True at/after the force-flat time — no overnight/extended holds."""
    return _et_time(ms) >= _parse_hhmm(close_et)


def session_flags(ms: int | None, params) -> tuple[bool, bool]:
    """``(can_open, must_flat)`` for a bar, honoring ``use_rth_filter``.

    With the filter off (or a missing timestamp) it degrades to the pre-RTH
    behavior: entries always allowed, never force-flat."""
    if not getattr(params, "use_rth_filter", False) or ms is None:
        return True, False
    return (can_open(ms, params.session_start_et, params.session_cutoff_et),
            must_flat(ms, params.session_close_et))
