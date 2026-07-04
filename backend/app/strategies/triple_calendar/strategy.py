"""Triple Calendar core: entry construction + exit/adjustment management. Pure given
a price source (underlying_close / chain_strikes / option_close). Shared by the
backtest and, in Phase 2, the live worker."""
from __future__ import annotations

from datetime import date, timedelta

from app.strategies.triple_calendar.legs import (Calendar, Position, add_calendar,
                                                 atm_strike, breached, expected_move,
                                                 outer_strikes, snap, target_expiry)


def calendar_debit(source, underlying, cal: Calendar, day: date) -> float | None:
    """Net debit/value of one calendar = long(buy) − short(sell), using calls."""
    sell = source.option_close(underlying, cal.sell_expiry, cal.strike, "C", day)
    buy = source.option_close(underlying, cal.buy_expiry, cal.strike, "C", day)
    if sell is None or buy is None:
        return None
    return buy - sell


def position_value(source, position: Position, day: date) -> float | None:
    total = 0.0
    for cal in position.calendars:
        v = calendar_debit(source, position.underlying, cal, day)
        if v is None:
            return None
        total += v
    return total


def decide_entry(day: date, source, params, events: list[date], vix: float | None) -> Position | None:
    u = params.underlying
    # Hard VIX gate: skip entry when VIX is known and above the entry ceiling — wait for it
    # to fall to vix_entry_max or below. (vix=None is permissive here; the live loop enforces
    # fail-closed by not attempting entry when it can't read VIX.)
    if vix is not None and vix > params.vix_entry_max:
        return None
    spot = source.underlying_close(u, day)
    if spot is None:
        return None
    sell_expiry = target_expiry(day, params.sell_dte)
    buy_expiry = target_expiry(sell_expiry + timedelta(days=7), 0)  # next Friday after sell
    # Event filter: block if a macro event falls while the (sell) leg is open.
    if any(day < e <= sell_expiry for e in events):
        return None
    strikes = source.chain_strikes(u, sell_expiry)
    atm = snap(atm_strike(spot, params.strike_round), strikes)
    call = source.option_close(u, sell_expiry, atm, "C", day)
    put = source.option_close(u, sell_expiry, atm, "P", day)
    if call is None or put is None:
        return None
    em = expected_move(call, put)
    lo, up = outer_strikes(atm, em, params.em_buffer, params.strike_round, params.manual_nudge)
    chosen = sorted({snap(s, strikes) for s in (lo, atm, up)})
    cals = [Calendar(s, sell_expiry, buy_expiry) for s in chosen]
    pos = Position(underlying=u, entry_date=day, calendars=cals)
    debit = position_value(source, pos, day)
    if debit is None or debit <= 0:
        return None
    pos.net_debit = debit
    pos.size = params.vix_scale if (vix is not None and vix < params.vix_threshold) else 1.0
    return pos


def manage(position: Position, day: date, source, params) -> tuple[str, dict]:
    """Return ('exit'|'adjust'|'hold', info). Order: profit target, stop-loss, breach."""
    value = position_value(source, position, day)
    if value is not None and position.net_debit > 0:
        pnl_pct = (value - position.net_debit) / position.net_debit
        if pnl_pct >= params.profit_target:
            return "exit", {"reason": "target", "value": value, "pnl_pct": pnl_pct}
        if pnl_pct <= -params.stop_loss_pct:
            return "exit", {"reason": "stop_loss", "value": value, "pnl_pct": pnl_pct}
    sell_expiry = position.calendars[0].sell_expiry
    spot = source.underlying_close(position.underlying, day)
    if spot is not None:
        side = breached(position, spot)
        if side:
            cal = add_calendar(position, side, sell_expiry, position.calendars[0].buy_expiry, params)
            if cal is not None:
                add = calendar_debit(source, position.underlying, cal, day) or 0.0
                position.net_debit += add
                return "adjust", {"side": side, "strike": cal.strike}
    return "hold", {"value": value}
