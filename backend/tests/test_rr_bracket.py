"""Tests for the fixed-RR bracket resolver and EMA signal helper used by the
1:3 risk-reward comparison. Deterministic."""

from __future__ import annotations


def _bar(c_o, h, l, c):
    return {"open": c_o, "high": h, "low": l, "close": c, "date": "2025-01-01"}


def test_resolve_bracket_target_hit_is_win():
    from app.strategies.rr_test.bracket import resolve_bracket
    bars = [_bar(10, 10, 10, 10),                 # entry bar context
            _bar(10, 10.2, 9.9, 10.1),            # neither
            _bar(10.1, 14.0, 10.0, 13.5)]         # high 14 >= target 13 -> win
    # entry_fill 10, R=1 -> stop 9, target 13
    ex_price, reason, pnl_r = resolve_bracket(bars, 1, entry_fill=10.0, R=1.0, slip=0.0)
    assert reason == "target" and ex_price == 13.0 and pnl_r == 3.0


def test_resolve_bracket_stop_hit_is_loss():
    from app.strategies.rr_test.bracket import resolve_bracket
    bars = [_bar(10, 10, 10, 10),
            _bar(10, 10.1, 8.5, 9.0)]             # low 8.5 <= stop 9 -> loss
    ex_price, reason, pnl_r = resolve_bracket(bars, 1, entry_fill=10.0, R=1.0, slip=0.0)
    assert reason == "stop" and ex_price == 9.0 and pnl_r == -1.0


def test_resolve_bracket_stop_wins_ties():
    from app.strategies.rr_test.bracket import resolve_bracket
    # Same bar touches both stop (9) and target (13); stop must win (pessimistic).
    bars = [_bar(10, 10, 10, 10),
            _bar(10, 14.0, 8.0, 11.0)]
    _ex, reason, pnl_r = resolve_bracket(bars, 1, entry_fill=10.0, R=1.0, slip=0.0)
    assert reason == "stop" and pnl_r == -1.0


def test_ema_basic():
    from app.strategies.rr_test.bracket import ema
    e = ema([1, 2, 3, 4, 5], 3)        # multiplier 0.5
    assert e[0] == 1.0
    assert abs(e[1] - 1.5) < 1e-9      # 2*.5 + 1*.5
    assert abs(e[2] - 2.25) < 1e-9     # 3*.5 + 1.5*.5
