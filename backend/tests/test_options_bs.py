import math

from app.options.blackscholes import bs_price, norm_cdf


def test_norm_cdf_center_and_tails():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-9
    assert norm_cdf(-8) < 1e-6 and norm_cdf(8) > 1 - 1e-6


def test_expiry_is_intrinsic():
    assert bs_price(110, 100, 0, 0.2, 0.04, "C") == 10
    assert bs_price(90, 100, 0, 0.2, 0.04, "P") == 10
    assert bs_price(90, 100, 0, 0.2, 0.04, "C") == 0


def test_put_call_parity():
    S, K, T, iv, r = 100, 100, 0.5, 0.25, 0.04
    c = bs_price(S, K, T, iv, r, "C")
    p = bs_price(S, K, T, iv, r, "P")
    # C - P == S - K*e^{-rT}
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-6
    assert c > 0 and p > 0
