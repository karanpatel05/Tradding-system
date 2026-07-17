"""Pure Black-Scholes pricer (European, no dividends). No I/O — unit-tested in isolation.
Used by the options builder's payoff diagram to value still-open legs at a target date."""
from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(S: float, K: float, T: float, iv: float, r: float, kind: str) -> dict:
    """Analytic greeks. theta is per CALENDAR DAY; vega per 1 vol POINT (0.01).
    kind 'S' (stock): delta 1, others 0. T<=0: intrinsic deltas, others 0."""
    kind = kind.upper()
    if kind == "S":
        return {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if T <= 0 or iv <= 0:
        if kind == "C":
            d = 1.0 if S > K else 0.0
        else:
            d = -1.0 if S < K else 0.0
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf = norm_pdf(d1)
    gamma = pdf / (S * iv * sqrt_t)
    vega = S * pdf * sqrt_t / 100.0
    if kind == "C":
        delta = norm_cdf(d1)
        theta_y = -S * pdf * iv / (2 * sqrt_t) - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        delta = norm_cdf(d1) - 1.0
        theta_y = -S * pdf * iv / (2 * sqrt_t) + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta_y / 365.0, "vega": vega}


def bs_price(S: float, K: float, T: float, iv: float, r: float, kind: str) -> float:
    """European call/put price. T in years; T<=0 or iv<=0 collapses to intrinsic value."""
    kind = kind.upper()
    if T <= 0 or iv <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if kind == "C":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float, kind: str,
                lo: float = 0.01, hi: float = 4.0, tol: float = 1e-4,
                max_iter: int = 60) -> float | None:
    """Invert bs_price by bisection. None when no root brackets [lo, hi]."""
    if price <= 0 or T <= 0:
        return None
    f_lo = bs_price(S, K, T, lo, r, kind) - price
    f_hi = bs_price(S, K, T, hi, r, kind) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = bs_price(S, K, T, mid, r, kind) - price
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2
