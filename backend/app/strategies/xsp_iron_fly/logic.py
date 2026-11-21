"""Pure iron-fly math: strike/wing selection, credit, max loss, sizing, stops, settlement.

Everything here is deterministic and side-effect free so it can be unit-tested
exhaustively. Prices are per-share index points; dollar values multiply by
``multiplier`` (100 for XSP).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-9

ROLES = ("short_call", "short_put", "long_call", "long_put")
SHORT_ROLES = ("short_call", "short_put")
LONG_ROLES = ("long_call", "long_put")


# --- ticks / quotes -------------------------------------------------------------
def round_to_tick(x: float, tick: float, direction: str = "nearest") -> float:
    n = x / tick
    if direction == "up":
        n = math.ceil(n - EPS)
    elif direction == "down":
        n = math.floor(n + EPS)
    else:
        n = math.floor(n + 0.5)
    return round(n * tick, 10)


def quote_valid(bid: float | None, ask: float | None) -> bool:
    """A usable two-sided option quote: ask > 0, bid >= 0 (far OTM bids can be 0),
    not crossed. No jump/width filtering: a stop must see a real gap."""
    return (bid is not None and ask is not None and ask > 0 and bid >= 0
            and ask >= bid)


def mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2.0


def marketable_limit(side: str, bid: float, ask: float, through: float,
                     tick: float) -> float | None:
    """Limit a few cents through the touch. BUY: ask + through (rounded up).
    SELL: bid - through (rounded down, floored at one tick). None for a SELL with
    no bid (nothing to hit)."""
    if side == "BUY":
        return round_to_tick(ask + through, tick, "up")
    if bid <= 0:
        return None
    return max(tick, round_to_tick(bid - through, tick, "down"))


# --- strike selection -------------------------------------------------------------
def select_body_strike(spot: float, strikes: list[float]) -> float | None:
    """Spot rounded to the nearest listed strike; an exact tie (spot at x.50 with both
    neighbours listed) rounds UP, matching round-half-up to the nearest $1."""
    if not strikes or spot is None or spot <= 0:
        return None
    return min(strikes, key=lambda k: (abs(k - spot), -k))


def select_wings(body: float, width: float, strikes: list[float],
                 put_strikes: list[float] | None = None) -> tuple[float, float] | None:
    """(call_wing, put_wing): the listed strike nearest body+W (strictly above the
    body) and nearest body-W (strictly below). A tie picks the strike closer to the
    body (narrower wing, smaller max loss). None if either side has no strike.
    ``put_strikes`` (default: ``strikes``) lists the strikes with a listed put."""
    above = [k for k in strikes if k > body + EPS]
    below = [k for k in (strikes if put_strikes is None else put_strikes)
             if k < body - EPS]
    if not above or not below:
        return None
    call = min(above, key=lambda k: (abs(k - (body + width)), k))
    put = min(below, key=lambda k: (abs(k - (body - width)), -k))
    return call, put


def build_fly(body: float, call_wing: float, put_wing: float) -> list[dict]:
    """The four legs, role-tagged. Order matters only for display."""
    return [
        {"role": "short_call", "right": "C", "strike": body, "action": "SELL"},
        {"role": "short_put", "right": "P", "strike": body, "action": "SELL"},
        {"role": "long_call", "right": "C", "strike": call_wing, "action": "BUY"},
        {"role": "long_put", "right": "P", "strike": put_wing, "action": "BUY"},
    ]


# --- credit / risk ----------------------------------------------------------------
def combo_credits(quotes: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """(mid_credit, natural_credit) per 1x from {role: (bid, ask)}. Natural = sell
    the shorts at the bid, buy the wings at the ask (the marketable price)."""
    mid_c = (sum(mid(*quotes[r]) for r in SHORT_ROLES)
             - sum(mid(*quotes[r]) for r in LONG_ROLES))
    nat_c = (sum(quotes[r][0] for r in SHORT_ROLES)
             - sum(quotes[r][1] for r in LONG_ROLES))
    return mid_c, nat_c


def net_credit(fills: dict[str, float]) -> float:
    """Body credit minus wing debit, from per-leg fill prices {role: price}."""
    return (fills["short_call"] + fills["short_put"]
            - fills["long_call"] - fills["long_put"])


def wing_widths(body: float, call_wing: float, put_wing: float) -> tuple[float, float]:
    return call_wing - body, body - put_wing


def max_loss_per_fly(call_width: float, put_width: float, credit: float,
                     multiplier: int = 100) -> float:
    """Defined max loss in $ per fly: (widest wing - net credit) x multiplier. With
    asymmetric wings (a substituted strike) only the wider side can be fully lost."""
    return max(0.0, (max(call_width, put_width) - credit) * multiplier)


@dataclass(frozen=True)
class Sizing:
    quantity: int
    reason: str            # "" when quantity >= 1
    by_capital: int
    by_buying_power: int
    by_daily_loss: int
    by_max_contracts: int


def size_flies(capital: float, buying_power: float, max_loss: float, *,
               capital_per_fly: float, max_contracts: int,
               max_daily_loss: float) -> Sizing:
    """Flies = min(capital / capital_per_fly, buying_power / max_loss,
    max_daily_loss / max_loss, max_contracts), floored. The daily-loss term keeps a
    full max loss on the day inside the circuit breaker."""
    if max_loss <= 0:
        return Sizing(0, "non-positive max loss (bad quotes)", 0, 0, 0, max_contracts)
    by_cap = int(math.floor(capital / capital_per_fly + EPS)) if capital > 0 else 0
    by_bp = int(math.floor(buying_power / max_loss + EPS)) if buying_power > 0 else 0
    by_dl = int(math.floor(max_daily_loss / max_loss + EPS))
    q = max(0, min(by_cap, by_bp, by_dl, max_contracts))
    reason = ""
    if q == 0:
        if by_cap == 0:
            reason = f"capital {capital:.0f} < capital_per_fly {capital_per_fly:.0f}"
        elif by_bp == 0:
            reason = f"buying power {buying_power:.0f} < max loss {max_loss:.0f}"
        elif by_dl == 0:
            reason = f"max loss {max_loss:.0f} per fly exceeds max_daily_loss {max_daily_loss:.0f}"
        else:
            reason = "max_contracts is 0"
    return Sizing(q, reason, by_cap, by_bp, by_dl, max_contracts)


def entry_credit_ladder(mid_credit: float, *, step: float, max_concession: float,
                        tick: float) -> list[float]:
    """Descending net-credit limits: the mid (rounded down to tick), then one step of
    concession at a time, never giving up more than ``max_concession`` vs the mid."""
    start = round_to_tick(mid_credit, tick, "down")
    floor = mid_credit - max_concession
    out = [start]
    x = start
    while True:
        x = round_to_tick(x - step, tick, "down")
        if x < floor - EPS:
            break
        out.append(x)
    return out


# --- stops ------------------------------------------------------------------------
def stop_level(entry_fill: float, mult: float) -> float:
    return entry_fill * mult


def stop_price(bid: float | None, ask: float | None, trigger: str) -> float | None:
    """The buy-to-close price a stop compares against, or None for an unusable quote."""
    if not quote_valid(bid, ask):
        return None
    return ask if trigger == "ask" else mid(bid, ask)


def stop_triggered(bid: float | None, ask: float | None, level: float,
                   trigger: str = "ask") -> bool:
    """True when the short's buy-to-close price is at or above its stop level. A gap
    straight through the level triggers too (and fills worse; that's expected)."""
    p = stop_price(bid, ask, trigger)
    return p is not None and p >= level - EPS


# --- settlement / P&L -------------------------------------------------------------
def intrinsic(right: str, strike: float, settle: float) -> float:
    return max(0.0, settle - strike) if right == "C" else max(0.0, strike - settle)


def leg_gross_pnl(action: str, entry: float, exit_: float, qty: int,
                  multiplier: int = 100) -> float:
    """$ P&L of one leg before fees. A SELL profits when it is bought back cheaper."""
    sign = -1 if action == "SELL" else 1
    return sign * (exit_ - entry) * qty * multiplier


def fly_settlement_pnl(legs: list[dict], settle: float, qty: int,
                       multiplier: int = 100) -> float:
    """Gross $ P&L if every leg is held to cash settlement. legs need right, strike,
    action and entry_price."""
    return sum(leg_gross_pnl(l["action"], l["entry_price"],
                             intrinsic(l["right"], l["strike"], settle), qty, multiplier)
               for l in legs)


def mark_to_market(legs: list[dict], marks: dict[str, float | None],
                   multiplier: int = 100) -> float | None:
    """Gross $ P&L of the session: realized legs at their exit, open legs at the mark.
    None when an open leg has no mark (unknown is not zero)."""
    total = 0.0
    for l in legs:
        qty = int(l.get("qty") or 0)
        closed = int(l.get("exit_qty") or 0)
        if closed:
            total += leg_gross_pnl(l["action"], l["entry_price"], l["exit_price"],
                                   closed, multiplier)
        open_q = qty - closed
        if open_q > 0:
            m = marks.get(l["role"])
            if m is None:
                return None
            total += leg_gross_pnl(l["action"], l["entry_price"], m, open_q, multiplier)
    return total
