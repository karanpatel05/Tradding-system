"""European Black-Scholes price + implied-vol inversion (bisection). Used to model
IV from Polygon's option closes for the regime breakdown."""
from __future__ import annotations

from math import erf, exp, log, sqrt


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_price(kind: str, S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if kind == "C" else max(K - S, 0.0)
    d1 = (log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if kind == "C":
        return S * exp(-q * T) * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
    return K * exp(-r * T) * _norm_cdf(-d2) - S * exp(-q * T) * _norm_cdf(-d1)


def implied_vol(price: float, kind: str, S: float, K: float, T: float, r: float,
                q: float = 0.0) -> float | None:
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(kind, S, K, T, r, hi, q) < price:   # price above what 500% vol can produce
        return None
    for _ in range(100):
        mid = (lo + hi) / 2.0
        diff = bs_price(kind, S, K, T, r, mid, q) - price
        if abs(diff) < 1e-6:
            return mid
        if diff < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
