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


def fit_iv(legs: list[dict], spot: float | None, live_value: float | None, *,
           today: date | None = None, default: float = DEFAULT_IV, r: float = RATE,
           lo: float = 0.01, hi: float = 3.0, iters: int = 60) -> tuple[float, str]:
    """Single IV that reprices the structure to its live mark today (both legs alive).
    The long far-dated leg dominates vega, so structure value is monotincreasing in IV
    and bisection converges. Fallback to `default`/"assumed" when unfittable."""
    from app.options.combo import payoff_at

    if not legs or spot is None or spot <= 0 or live_value is None:
        return default, "assumed"
    today = today or date.today()

    def f(v: float) -> float:
        return payoff_at(legs, spot, today, v, r) - live_value

    flo, fhi = f(lo), f(hi)
    if flo == 0.0:
        return lo, "fit"
    if fhi == 0.0:
        return hi, "fit"
    if (flo > 0) == (fhi > 0):
        return default, "assumed"  # live value outside the model's [lo, hi] range
    a, b = lo, hi
    for _ in range(iters):
        m = (a + b) / 2
        fm = f(m)
        if fm == 0.0:
            return round(m, 4), "fit"
        if (fm > 0) == (flo > 0):
            a, flo = m, fm
        else:
            b = m
    return round((a + b) / 2, 4), "fit"
