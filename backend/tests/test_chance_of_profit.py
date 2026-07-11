"""chance_of_profit: lognormal probability mass over the curve's profitable regions.
Hand-computed expectations with S0=100, iv=0.20, r=0, T=0.25 (sigma*sqrt(T)=0.1,
mu=(0 - 0.02)*0.25 = -0.005):
  P(S<=x) = N((ln(x/100) + 0.005) / 0.1)
"""
import pytest

from app.options.combo import chance_of_profit

S0, IV, R, T = 100.0, 0.20, 0.0, 0.25


def test_long_call_profit_above_breakeven():
    # Profitable above 102: chance = 1 - N((ln(1.02)+0.005)/0.1) = 1 - N(0.24803) ~ 0.4021
    curve = [{"price": 80.0, "pnl": -220.0}, {"price": 91.0, "pnl": -110.0},
             {"price": 113.0, "pnl": 110.0}, {"price": 124.0, "pnl": 220.0}]
    # crossing at 102 (linear): between 91 (-110) and 113 (+110) -> t=0.5 -> 102
    cop = chance_of_profit(curve, S0, IV, R, T)
    assert cop == pytest.approx(0.402, abs=2e-3)


def test_bounded_hump_profit_between_breakevens():
    # Profitable between 95 and 105: N(z105) - N(z95) with
    # z95=(ln(0.95)+0.005)/0.1=-0.4629 -> 0.3217 ; z105=(ln(1.05)+0.005)/0.1=0.5379 -> 0.7047
    curve = [{"price": 85.0, "pnl": -100.0}, {"price": 95.0, "pnl": 0.0},
             {"price": 100.0, "pnl": 300.0}, {"price": 105.0, "pnl": 0.0},
             {"price": 115.0, "pnl": -100.0}]
    cop = chance_of_profit(curve, S0, IV, R, T)
    assert cop == pytest.approx(0.7047 - 0.3217, abs=3e-3)


def test_degenerate_inputs():
    curve = [{"price": 90.0, "pnl": -100.0}, {"price": 110.0, "pnl": 100.0}]
    assert chance_of_profit(curve, None, IV, R, T) is None      # no spot
    assert chance_of_profit(curve, S0, 0.0, R, T) is None       # no vol
    assert chance_of_profit([], S0, IV, R, T) is None           # empty curve
    # T=0: deterministic at spot (pnl at 100 ~ 0 -> not > 0 -> 0.0)
    assert chance_of_profit(curve, S0, IV, R, 0.0) == 0.0
    assert chance_of_profit(curve, 109.0, IV, R, 0.0) == 1.0
