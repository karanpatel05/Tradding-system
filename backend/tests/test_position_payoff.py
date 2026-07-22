from datetime import date
from types import SimpleNamespace

from app.options.combo import payoff_at
from app.options.position_payoff import (
    build_position_payoff,
    fit_iv,
    legs_from_calendars,
    net_position_greeks,
)

TODAY = date(2026, 7, 21)
CALS = [
    {"strike": 470.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 480.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 490.0, "sell": "2026-08-21", "buy": "2026-09-18"},
]


def test_legs_from_calendars_builds_sell_near_buy_far_calls():
    cals = [
        {"strike": 470.0, "sell": "2026-08-15", "buy": "2026-09-19"},
        {"strike": 480.0, "sell": "2026-08-15", "buy": "2026-09-19"},
    ]
    legs = legs_from_calendars(cals)
    assert legs == [
        {"action": "SELL", "right": "C", "strike": 470.0, "expiry": "2026-08-15", "qty": 1},
        {"action": "BUY",  "right": "C", "strike": 470.0, "expiry": "2026-09-19", "qty": 1},
        {"action": "SELL", "right": "C", "strike": 480.0, "expiry": "2026-08-15", "qty": 1},
        {"action": "BUY",  "right": "C", "strike": 480.0, "expiry": "2026-09-19", "qty": 1},
    ]


def test_legs_from_calendars_empty():
    assert legs_from_calendars([]) == []
    assert legs_from_calendars(None) == []


def test_fit_iv_round_trips_to_live_value():
    legs = legs_from_calendars(CALS)
    spot = 480.0
    true_iv = 0.22
    live_value = payoff_at(legs, spot, TODAY, true_iv, 0.04)
    iv, source = fit_iv(legs, spot, live_value, today=TODAY)
    assert source == "fit"
    assert abs(iv - true_iv) < 1e-3


def test_fit_iv_falls_back_to_assumed_when_no_live_value():
    legs = legs_from_calendars(CALS)
    assert fit_iv(legs, 480.0, None, today=TODAY) == (0.25, "assumed")
    assert fit_iv(legs, None, 4.0, today=TODAY) == (0.25, "assumed")


def test_net_greeks_scale_linearly_with_contracts():
    legs = legs_from_calendars(CALS)
    g1 = net_position_greeks(legs, 480.0, 0.22, 1, today=TODAY)
    g2 = net_position_greeks(legs, 480.0, 0.22, 2, today=TODAY)
    assert set(g1) == {"delta", "gamma", "theta", "vega"}
    for k in g1:
        assert abs(g2[k] - 2 * g1[k]) < 1e-6
    # A long calendar is net long vega (far leg dominates).
    assert g1["vega"] > 0


def test_net_greeks_zero_without_spot():
    legs = legs_from_calendars(CALS)
    assert net_position_greeks(legs, None, 0.22, 1, today=TODAY) == {
        "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


def _rec(**kw):
    base = dict(calendars=CALS, net_debit=4.0, last_spot=480.0, last_value=4.6,
                underlying="QQQ")
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_payoff_full_shape_and_scaling():
    out1 = build_position_payoff(_rec(), 1, today=TODAY)
    assert out1["has_position"] is True
    assert out1["ticker"] == "QQQ"
    assert out1["eval_date"] == "2026-08-21"      # nearest sell expiry
    assert out1["iv_source"] == "fit"
    assert out1["strikes"] == [470.0, 480.0, 490.0]
    assert len(out1["curve"]) == 61
    # current mark P&L = (live_value - net_debit) * 100 * contracts
    assert out1["current_pnl"] == round((4.6 - 4.0) * 100 * 1, 2)
    assert 0.0 <= out1["chance_of_profit"] <= 1.0
    # A long calendar peaks near the middle strike (positive max profit).
    assert out1["max_profit"] > 0

    out2 = build_position_payoff(_rec(), 2, today=TODAY)
    assert out2["current_pnl"] == round((4.6 - 4.0) * 100 * 2, 2)
    assert abs(out2["max_profit"] - 2 * out1["max_profit"]) < 0.05  # dollar stats ×contracts
    assert out2["strikes"] == out1["strikes"]                        # price-space unchanged


def test_build_payoff_flat_when_no_calendars():
    assert build_position_payoff(_rec(calendars=[]), 1, today=TODAY) == {
        "has_position": False, "ticker": "QQQ"}
