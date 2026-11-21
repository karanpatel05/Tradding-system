"""Pure XSP iron-fly math: strikes, wings, credit, max loss, sizing, stops, settlement.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_xsp.db ../.venv/bin/python -m pytest tests/test_xsp_fly_logic.py
"""
from __future__ import annotations

from datetime import date

import pytest

from app.strategies.xsp_iron_fly import calendar as cal
from app.strategies.xsp_iron_fly import logic as L
from app.strategies.xsp_iron_fly.params import IronFlyParams

STRIKES = [float(k) for k in range(580, 621)]


# --- strike / wing selection ---------------------------------------------------
@pytest.mark.parametrize("spot,body", [(600.0, 600.0), (600.49, 600.0), (600.51, 601.0),
                                       (599.6, 600.0)])
def test_body_rounds_to_nearest_dollar(spot, body):
    assert L.select_body_strike(spot, STRIKES) == body


def test_body_tie_at_half_rounds_up():
    assert L.select_body_strike(600.5, STRIKES) == 601.0


def test_body_uses_nearest_listed_when_dollar_strike_missing():
    ks = [595.0, 600.0, 605.0]
    assert L.select_body_strike(601.9, ks) == 600.0
    assert L.select_body_strike(603.0, ks) == 605.0


def test_body_none_without_spot_or_strikes():
    assert L.select_body_strike(None, STRIKES) is None
    assert L.select_body_strike(600.0, []) is None


def test_wings_exact_width():
    assert L.select_wings(600.0, 10, STRIKES) == (610.0, 590.0)


def test_wings_substitute_nearest_listed_and_recompute_width():
    ks = [585.0, 588.0, 600.0, 609.0, 613.0]
    call, put = L.select_wings(600.0, 10, ks)
    assert (call, put) == (609.0, 588.0)
    assert L.wing_widths(600.0, call, put) == (9.0, 12.0)


def test_wing_tie_prefers_narrower():
    ks = [589.0, 591.0, 600.0, 609.0, 611.0]
    assert L.select_wings(600.0, 10, ks) == (609.0, 591.0)


def test_wings_never_on_the_body_or_wrong_side():
    assert L.select_wings(600.0, 10, [600.0, 610.0]) is None   # no put side
    call, put = L.select_wings(600.0, 10, [599.0, 600.0, 601.0])
    assert call > 600.0 > put


def test_build_fly_legs():
    legs = L.build_fly(600.0, 610.0, 590.0)
    assert [(l["role"], l["right"], l["strike"], l["action"]) for l in legs] == [
        ("short_call", "C", 600.0, "SELL"), ("short_put", "P", 600.0, "SELL"),
        ("long_call", "C", 610.0, "BUY"), ("long_put", "P", 590.0, "BUY")]


# --- credit / max loss ------------------------------------------------------------
def test_net_credit_is_body_credit_minus_wing_debit():
    fills = {"short_call": 2.40, "short_put": 2.10, "long_call": 0.35, "long_put": 0.45}
    assert L.net_credit(fills) == pytest.approx(3.70)


def test_combo_credits_mid_and_natural():
    q = {"short_call": (2.35, 2.45), "short_put": (2.05, 2.15),
         "long_call": (0.30, 0.40), "long_put": (0.40, 0.50)}
    mid_c, nat_c = L.combo_credits(q)
    assert mid_c == pytest.approx(2.40 + 2.10 - 0.35 - 0.45)
    assert nat_c == pytest.approx(2.35 + 2.05 - 0.40 - 0.50)


def test_max_loss_symmetric_and_asymmetric():
    assert L.max_loss_per_fly(10, 10, 3.0) == pytest.approx(700.0)
    assert L.max_loss_per_fly(9, 12, 3.0) == pytest.approx(900.0)   # wider side
    assert L.max_loss_per_fly(10, 10, 11.0) == 0.0                   # never negative


# --- sizing ---------------------------------------------------------------------
def _size(capital, bp, ml, **kw):
    args = dict(capital_per_fly=5000, max_contracts=10, max_daily_loss=10_000)
    args.update(kw)
    return L.size_flies(capital, bp, ml, **args)


def test_sizing_one_fly_per_5k():
    assert _size(5000, 1e9, 700).quantity == 1
    assert _size(14_999, 1e9, 700).quantity == 2
    assert _size(4_999, 1e9, 700).quantity == 0


def test_sizing_floored_by_buying_power_and_max_contracts():
    assert _size(50_000, 1500, 700).quantity == 2
    assert _size(50_000, 1e9, 700, max_contracts=3).quantity == 3


def test_sizing_capped_by_daily_loss():
    s = _size(50_000, 1e9, 700, max_daily_loss=1500)
    assert s.quantity == 2 and s.by_daily_loss == 2
    s = _size(50_000, 1e9, 900, max_daily_loss=800)
    assert s.quantity == 0 and "max_daily_loss" in s.reason


def test_sizing_rejects_bad_max_loss():
    assert _size(50_000, 1e9, 0).quantity == 0


def test_entry_credit_ladder_bounded_by_concession():
    ladder = L.entry_credit_ladder(3.737, step=0.05, max_concession=0.30, tick=0.01)
    assert ladder[0] == pytest.approx(3.73)
    assert all(a > b for a, b in zip(ladder, ladder[1:]))
    assert ladder[-1] >= 3.737 - 0.30 - 1e-9


# --- marketable limits ---------------------------------------------------------
def test_marketable_limit_goes_through_the_touch():
    assert L.marketable_limit("BUY", 2.00, 2.10, 0.05, 0.01) == pytest.approx(2.15)
    assert L.marketable_limit("SELL", 2.00, 2.10, 0.05, 0.01) == pytest.approx(1.95)
    assert L.marketable_limit("SELL", 0.0, 0.05, 0.05, 0.01) is None
    assert L.marketable_limit("SELL", 0.03, 0.05, 0.05, 0.01) == pytest.approx(0.01)


# --- stops ------------------------------------------------------------------------
def test_stop_level_is_1_5x_fill():
    assert L.stop_level(2.40, 1.5) == pytest.approx(3.60)


def test_stop_triggers_at_level_on_ask():
    assert not L.stop_triggered(3.40, 3.59, 3.60)
    assert L.stop_triggered(3.50, 3.60, 3.60)


def test_stop_gap_through_triggers():
    # A single tick from well below to far above (+80%): must fire, never be filtered.
    assert L.stop_triggered(4.20, 4.32, 3.60)


def test_stop_mark_trigger_mode():
    assert not L.stop_triggered(3.40, 3.70, 3.60, "mark")    # mark 3.55
    assert L.stop_triggered(3.50, 3.70, 3.60, "mark")        # mark 3.60


def test_stop_ignores_unusable_quotes():
    assert not L.stop_triggered(None, 9.0, 3.60)
    assert not L.stop_triggered(5.0, 4.0, 3.60)       # crossed
    assert not L.stop_triggered(0.0, 0.0, 3.60)


# --- settlement -------------------------------------------------------------------
def _legs():
    legs = L.build_fly(600.0, 610.0, 590.0)
    for l, p in zip(legs, (2.40, 2.10, 0.35, 0.45)):
        l["entry_price"] = p
    return legs


def test_intrinsic():
    assert L.intrinsic("C", 600, 605.5) == pytest.approx(5.5)
    assert L.intrinsic("P", 600, 605.5) == 0.0
    assert L.intrinsic("P", 600, 590) == pytest.approx(10.0)


def test_settlement_pnl_pin_is_full_credit():
    assert L.fly_settlement_pnl(_legs(), 600.0, 1) == pytest.approx(370.0)


def test_settlement_pnl_beyond_wing_is_max_loss():
    assert L.fly_settlement_pnl(_legs(), 625.0, 1) == pytest.approx(-630.0)
    assert L.fly_settlement_pnl(_legs(), 570.0, 2) == pytest.approx(-1260.0)
    assert L.max_loss_per_fly(10, 10, 3.70) == pytest.approx(630.0)


def test_settlement_pnl_inside_tent():
    assert L.fly_settlement_pnl(_legs(), 603.0, 1) == pytest.approx(70.0)


def test_mark_to_market_mixes_realized_and_open():
    legs = _legs()
    for l in legs:
        l["qty"] = 1
    legs[0].update(exit_qty=1, exit_price=3.60)       # call stopped: -120
    marks = {"short_put": 1.00, "long_call": 1.20, "long_put": 0.05}
    mtm = L.mark_to_market(legs, marks)
    assert mtm == pytest.approx(-120 + 110 + 85 - 40)
    assert L.mark_to_market(legs, {"short_put": None, "long_call": 1, "long_put": 1}) is None


# --- calendar -------------------------------------------------------------------
def test_calendar_holiday_half_day_and_unconfigured_year():
    assert cal.is_trading_day(date(2026, 11, 20))
    assert not cal.is_trading_day(date(2026, 11, 26))           # Thanksgiving
    assert cal.skip_reason(date(2026, 11, 26)) == "exchange holiday"
    assert cal.close_time(date(2026, 11, 27)).hour == 13          # half day
    assert cal.close_time(date(2026, 11, 30)).hour == 16
    assert not cal.is_trading_day(date(2028, 3, 1))              # fail closed
    assert "not configured" in cal.skip_reason(date(2028, 3, 1))


def test_default_params_are_spec_values_and_valid():
    p = IronFlyParams()
    assert (p.ticker, p.entry_time.strftime("%H:%M"), p.wing_width, p.stop_mult,
            p.capital_per_fly, p.close_before_settlement) == ("XSP", "09:45", 10.0, 1.5,
                                                              5000.0, False)
    assert p.validate() == []
    assert IronFlyParams(stop_trigger="bid").validate()
