"""Pure Black-Scholes pricer (European, no dividends). No I/O — unit-tested in isolation.
Used by the options builder's payoff diagram to value still-open legs at a target date."""
from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


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
