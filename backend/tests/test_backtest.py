"""Backtest metrics test (deterministic, pure-Python). Run:
   python -m tests.test_backtest
"""

from __future__ import annotations

import math

from app.backtest import metrics


def _run() -> None:
    returns = [0.10, -0.05, 0.20]

    assert abs(metrics.total_return(returns) - 0.254) < 1e-9, metrics.total_return(returns)
    assert abs(metrics.win_rate(returns) - 2 / 3) < 1e-9
    avg_win, avg_loss = metrics.avg_win_loss(returns)
    assert abs(avg_win - 0.15) < 1e-9, avg_win
    assert abs(avg_loss - (-0.05)) < 1e-9, avg_loss
    assert abs(metrics.expectancy(returns) - 0.25 / 3) < 1e-9
    # Drawdown: peak 1.1 -> 1.045 trough = -5%.
    assert abs(metrics.max_drawdown(returns) - (-0.05)) < 1e-9, metrics.max_drawdown(returns)
    assert math.isfinite(metrics.sharpe(returns))
    print("PASS: metrics on known 3-return series")

    # Graceful edge cases.
    assert metrics.summarize([])["trade_count"] == 0
    assert metrics.sharpe([0.01]) == 0.0  # <2 points
    assert metrics.max_drawdown([0.1, 0.1]) == 0.0  # monotonic up
    print("PASS: graceful edge cases (empty / single / monotonic)")

    # Equity curve compounds correctly.
    curve = metrics.equity_curve([0.1, 0.1])
    assert abs(curve[-1] - 1.21) < 1e-9, curve
    print("PASS: equity curve compounding")

    print("\nALL BACKTEST METRIC TESTS PASSED")


if __name__ == "__main__":
    _run()
