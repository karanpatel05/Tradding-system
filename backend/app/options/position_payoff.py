"""Turn an open Triple Calendar OptionPositionRecord into a payoff diagram + stats,
reusing the generic options math in app/options/combo.py and blackscholes.py. Pure:
no DB, no network — the router loads the record and passes it in."""
from __future__ import annotations

from datetime import date

RATE = 0.04
DEFAULT_IV = 0.25


def legs_from_calendars(calendars: list[dict] | None) -> list[dict]:
    """Each calendar {strike, sell, buy} -> a SELL near call + a BUY far call (TC is all
    calls). Leg dicts match the shape app/options/combo.py consumes."""
    legs: list[dict] = []
    for c in calendars or []:
        strike = float(c["strike"])
        legs.append({"action": "SELL", "right": "C", "strike": strike,
                     "expiry": c["sell"], "qty": 1})
        legs.append({"action": "BUY", "right": "C", "strike": strike,
                     "expiry": c["buy"], "qty": 1})
    return legs
