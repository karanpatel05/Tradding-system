"""Generic multi-leg options math (pure): current value from mids, and Black-Scholes payoff
curves. Structure-agnostic — works for any legs, unlike the calendar-specific TC logic."""
from __future__ import annotations

from datetime import date

from app.options.blackscholes import bs_price

LEG_SIGN = {"BUY": 1, "SELL": -1}


def _qty(leg: dict) -> int:
    return int(leg.get("qty", 1) or 1)


def _expiry(leg: dict) -> date:
    e = leg["expiry"]
    return e if isinstance(e, date) else date.fromisoformat(e)


def combo_value(legs: list[dict], mids: list[float | None]) -> float | None:
    """Per-1x value of the structure from leg mids (aligned by index). None if any mid missing.
    This is the entry net-debit at save (entry mids) and the live value at monitor time."""
    total = 0.0
    for leg, mid in zip(legs, mids):
        if mid is None:
            return None
        total += LEG_SIGN[leg["action"]] * _qty(leg) * mid
    return total


def payoff_at(legs: list[dict], S: float, eval_date: date, iv: float, r: float) -> float:
    """Structure value (per 1x) at underlying price S on eval_date. Expired legs collapse to
    intrinsic; still-open legs are Black-Scholes-valued (so calendars keep their tent)."""
    total = 0.0
    for leg in legs:
        T = max((_expiry(leg) - eval_date).days, 0) / 365.0
        price = bs_price(S, leg["strike"], T, iv, r, leg["right"])
        total += LEG_SIGN[leg["action"]] * _qty(leg) * price
    return total


def payoff_curve(legs: list[dict], net_debit: float, eval_date: date, iv: float, r: float,
                 lo: float, hi: float, points: int = 61) -> list[dict]:
    """[{price, pnl}] across [lo, hi]; pnl in dollars = (value - net_debit) * 100."""
    if points < 2:
        points = 2
    step = (hi - lo) / (points - 1)
    out = []
    for i in range(points):
        S = lo + step * i
        val = payoff_at(legs, S, eval_date, iv, r)
        out.append({"price": round(S, 2), "pnl": round((val - net_debit) * 100, 2)})
    return out


def curve_stats(curve: list[dict]) -> dict:
    """Max profit/loss and linear-interpolated breakeven crossings from a payoff curve."""
    pnls = [p["pnl"] for p in curve]
    breakevens = []
    for a, b in zip(curve, curve[1:]):
        if (a["pnl"] < 0 <= b["pnl"]) or (a["pnl"] > 0 >= b["pnl"]):
            if b["pnl"] != a["pnl"]:
                t = -a["pnl"] / (b["pnl"] - a["pnl"])
                breakevens.append(round(a["price"] + t * (b["price"] - a["price"]), 2))
    return {"max_profit": max(pnls), "max_loss": min(pnls), "breakevens": breakevens}


def exit_check(net_debit: float | None, value: float | None,
               target_pct: float | None, stop_pct: float | None) -> str | None:
    """'target' | 'stop' | None. Only debit structures (net_debit > 0) auto-exit on %."""
    if net_debit is None or net_debit <= 0 or value is None:
        return None
    pnl_pct = (value - net_debit) / net_debit
    if target_pct is not None and pnl_pct >= target_pct:
        return "target"
    if stop_pct is not None and pnl_pct <= -stop_pct:
        return "stop"
    return None
