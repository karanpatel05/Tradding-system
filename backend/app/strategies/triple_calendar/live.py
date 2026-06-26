"""Triple Calendar live management: turn positions into IBKR combo legs and drive
enter/adjust/exit. Reuses the Phase-1 core (decide_entry/manage). Paper-safe via the
execution gate."""
from __future__ import annotations

from datetime import date

from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.strategy import decide_entry, manage, position_value


def _calendar_legs(cal: Calendar, opening: bool) -> list[dict]:
    # Opening a calendar: SELL the near, BUY the far. Closing reverses both.
    near = "SELL" if opening else "BUY"
    far = "BUY" if opening else "SELL"
    return [
        {"expiry": cal.sell_expiry, "strike": cal.strike, "right": "C", "action": near},
        {"expiry": cal.buy_expiry, "strike": cal.strike, "right": "C", "action": far},
    ]


def entry_legs(position: Position) -> list[dict]:
    legs: list[dict] = []
    for cal in position.calendars:
        legs += _calendar_legs(cal, opening=True)
    return legs


def adjustment_legs(cal: Calendar) -> list[dict]:
    return _calendar_legs(cal, opening=True)


def exit_legs(position: Position) -> list[dict]:
    legs: list[dict] = []
    for cal in position.calendars:
        legs += _calendar_legs(cal, opening=False)
    return legs


async def manage_step(day: date, source, params, executor, position: Position | None,
                      events=None, vix=None) -> dict:
    """One live tick. `executor(underlying, legs, *, net_price, action, reason)` places a
    combo (or paper-logs it). Returns {action, position, info}."""
    events = events if events is not None else params.event_dates
    if position is None:
        pos = decide_entry(day, source, params, events, vix)
        if pos is None:
            return {"action": "none", "position": None, "info": {}}
        await executor(pos.underlying, entry_legs(pos), net_price=round(pos.net_debit, 2),
                       action="BUY", reason="entry")
        return {"action": "enter", "position": pos, "info": {"net_debit": pos.net_debit}}

    act, info = manage(position, day, source, params)
    if act == "adjust":
        cal = position.calendars[-1]   # manage() appended the new calendar
        await executor(position.underlying, adjustment_legs(cal), net_price=None,
                       action="BUY", reason="adjust")
        return {"action": "adjust", "position": position, "info": info}
    if act == "exit":
        value = info.get("value")
        if value is None:
            value = position_value(source, position, day) or position.net_debit
        await executor(position.underlying, exit_legs(position), net_price=None,
                       action="SELL", reason=info.get("reason", "exit"))
        pnl = value - position.net_debit
        return {"action": "exit", "position": None, "info": {**info, "pnl": pnl}}
    return {"action": "hold", "position": position, "info": info}
