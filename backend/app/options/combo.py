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
        # Stock legs: qty is SHARES, and the structure's per-1x unit is one option
        # contract (=100 shares) — scale by qty/100 so payoff_curve's ×100 yields
        # qty × price dollars (covered call per-1x debit = S − C, the classic form).
        scale = (_qty(leg) / 100.0) if leg["right"] == "S" else _qty(leg)
        total += LEG_SIGN[leg["action"]] * scale * mid
    return total


def payoff_at(legs: list[dict], S: float, eval_date: date, iv: float, r: float) -> float:
    """Structure value (per 1x) at underlying price S on eval_date. Expired legs collapse to
    intrinsic; still-open legs are Black-Scholes-valued (so calendars keep their tent)."""
    total = 0.0
    for leg in legs:
        if leg["right"] == "S":
            # Shares → per-1x structure units (100 shares = 1.0); see combo_value.
            total += LEG_SIGN[leg["action"]] * (_qty(leg) / 100.0) * S
            continue
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


def chance_of_profit(curve: list[dict], spot: float | None, iv: float, r: float,
                     T: float) -> float | None:
    """P(position pnl > 0 at the eval date) under the same lognormal model the pricer
    uses: ln(S_T/S0) ~ N((r - iv²/2)·T, iv²·T). Profitable price regions come from the
    curve's breakeven crossings; each region contributes its lognormal probability mass.
    None when it can't be computed (no spot / degenerate curve / no vol)."""
    import math

    from app.options.blackscholes import norm_cdf

    if not curve or len(curve) < 2 or spot is None or spot <= 0 or iv <= 0:
        return None
    if T <= 0:  # at the eval date: profit is deterministic at the current spot
        pnl = min(curve, key=lambda p: abs(p["price"] - spot))["pnl"]
        return 1.0 if pnl > 0 else 0.0
    mu = (r - 0.5 * iv * iv) * T
    sig = iv * math.sqrt(T)

    def cdf(price: float) -> float:  # P(S_T <= price)
        if price <= 0:
            return 0.0
        if math.isinf(price):
            return 1.0
        return norm_cdf((math.log(price / spot) - mu) / sig)

    bes = curve_stats(curve)["breakevens"]
    bounds = [0.0] + bes + [math.inf]
    total = 0.0
    lo_p, hi_p = curve[0]["price"], curve[-1]["price"]
    for lo, hi in zip(bounds, bounds[1:]):
        # Sample the curve inside the region to learn its sign (clamped to the curve's
        # sampled span; regions beyond it extend the edge sample's sign).
        test = (lo + hi) / 2 if math.isfinite(hi) else hi_p
        test = min(max(test, lo_p), hi_p)
        pnl = min(curve, key=lambda p: abs(p["price"] - test))["pnl"]
        if pnl > 0:
            total += cdf(hi) - cdf(lo)
    return round(min(1.0, max(0.0, total)), 4)
