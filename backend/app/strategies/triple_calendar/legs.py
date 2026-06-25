"""Triple Calendar position model + strike/expiry selection + breach/adjustment.
Pure (no I/O); shared by backtest now and live (Phase 2) later."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class Calendar:
    strike: float
    sell_expiry: date    # near-dated short leg
    buy_expiry: date     # longer-dated long leg


@dataclass
class Position:
    underlying: str
    entry_date: date
    calendars: list[Calendar] = field(default_factory=list)
    net_debit: float = 0.0
    size: float = 1.0


def nearest_friday(d: date) -> date:
    days = 4 - d.weekday()       # Friday == weekday 4
    if days > 3:
        days -= 7
    if days < -3:
        days += 7
    return d + timedelta(days=days)


def target_expiry(entry: date, dte: int) -> date:
    return nearest_friday(entry + timedelta(days=dte))


def atm_strike(price: float, round_to: float) -> float:
    return round(price / round_to) * round_to


def expected_move(call_mid: float, put_mid: float) -> float:
    return call_mid + put_mid


def outer_strikes(atm: float, em: float, buffer: float, round_to: float,
                  nudge: float = 0.0) -> tuple[float, float]:
    width = em + buffer + nudge
    lower = round((atm - width) / round_to) * round_to
    upper = round((atm + width) / round_to) * round_to
    return lower, upper


def snap(strike: float, available: list[float]) -> float:
    return min(available, key=lambda s: abs(s - strike)) if available else strike


def breached(position: Position, price: float) -> str | None:
    strikes = [c.strike for c in position.calendars]
    if price > max(strikes):
        return "upper"
    if price < min(strikes):
        return "lower"
    return None


def _spacing(strikes: list[float], default: float) -> float:
    s = sorted(strikes)
    return (s[-1] - s[0]) / (len(s) - 1) if len(s) > 1 else default


def add_calendar(position: Position, side: str, sell_expiry: date, buy_expiry: date,
                 params) -> Calendar | None:
    """Add one calendar further out on the breached side. Never removes existing ones.
    Returns the new Calendar, or None if the max-calendar cap is reached."""
    cap = params.max_calendars + (1 if params.allow_fifth else 0)
    if len(position.calendars) >= cap:
        return None
    strikes = [c.strike for c in position.calendars]
    spacing = _spacing(strikes, params.strike_round)
    raw = (max(strikes) + spacing) if side == "upper" else (min(strikes) - spacing)
    strike = round(raw / params.strike_round) * params.strike_round
    cal = Calendar(strike=strike, sell_expiry=sell_expiry, buy_expiry=buy_expiry)
    position.calendars.append(cal)
    return cal
