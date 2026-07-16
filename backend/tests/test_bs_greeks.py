"""Analytic BS greeks vs textbook values (S=100,K=100,T=0.25,iv=0.2,r=0.05):
d1=0.175, N(d1)=0.56946, phi(d1)=0.39288 — verified below with 2e-3 tolerances.
(Vega/gamma expectations corrected from the brief's draft after independent
verification: vega/point = 0.19644, gamma = 0.03929.)"""
import pytest

from app.options.blackscholes import bs_greeks


def test_call_greeks_textbook():
    g = bs_greeks(100.0, 100.0, 0.25, 0.20, 0.05, "C")
    assert g["delta"] == pytest.approx(0.5695, abs=2e-3)
    assert g["gamma"] == pytest.approx(0.0393, abs=2e-3)
    assert g["vega"] == pytest.approx(0.1964, abs=2e-3)     # per 1 vol POINT (=vega/100)
    assert g["theta"] == pytest.approx(-0.0284, abs=2e-3)   # per calendar DAY


def test_put_delta_parity():
    c = bs_greeks(100.0, 100.0, 0.25, 0.20, 0.05, "C")
    p = bs_greeks(100.0, 100.0, 0.25, 0.20, 0.05, "P")
    assert p["delta"] == pytest.approx(c["delta"] - 1.0, abs=1e-9)
    assert p["gamma"] == pytest.approx(c["gamma"], abs=1e-9)
    assert p["vega"] == pytest.approx(c["vega"], abs=1e-9)


def test_stock_and_expired():
    s = bs_greeks(100.0, 0.0, 0.0, 0.2, 0.05, "S")
    assert s == {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    e = bs_greeks(100.0, 90.0, 0.0, 0.2, 0.05, "C")   # expired ITM call -> delta 1
    assert e["delta"] == 1.0 and e["gamma"] == 0.0
