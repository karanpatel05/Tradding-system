"""Pure-Python performance metrics (no numpy — works anywhere).

Inputs are *period returns* expressed as fractions (0.01 == +1%). An "equity
curve" is the cumulative product of (1 + r), starting at 1.0.
"""

from __future__ import annotations

import math

TRADING_DAYS = 252


def equity_curve(returns: list[float], start: float = 1.0) -> list[float]:
    curve = [start]
    for r in returns:
        curve.append(curve[-1] * (1.0 + r))
    return curve


def total_return(returns: list[float]) -> float:
    eq = 1.0
    for r in returns:
        eq *= (1.0 + r)
    return eq - 1.0


def win_rate(returns: list[float]) -> float:
    if not returns:
        return 0.0
    wins = sum(1 for r in returns if r > 0)
    return wins / len(returns)


def avg_win_loss(returns: list[float]) -> tuple[float, float]:
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0  # negative
    return avg_win, avg_loss


def expectancy(returns: list[float]) -> float:
    """Average return per period (= win_rate*avg_win + loss_rate*avg_loss)."""
    if not returns:
        return 0.0
    return sum(returns) / len(returns)


def max_drawdown(returns: list[float]) -> float:
    """Largest peak-to-trough decline of the equity curve, as a negative fraction."""
    curve = equity_curve(returns)
    peak = curve[0]
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def sharpe(returns: list[float], periods_per_year: int = TRADING_DAYS,
           risk_free: float = 0.0) -> float:
    """Annualized Sharpe ratio. 0.0 when there's insufficient variance/data."""
    n = len(returns)
    if n < 2:
        return 0.0
    rf_per = risk_free / periods_per_year
    excess = [r - rf_per for r in returns]
    mean = sum(excess) / n
    var = sum((x - mean) ** 2 for x in excess) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def summarize(returns: list[float], periods_per_year: int = TRADING_DAYS) -> dict:
    avg_win, avg_loss = avg_win_loss(returns)
    return {
        "total_return": round(total_return(returns), 6),
        "win_rate": round(win_rate(returns), 4),
        "expectancy": round(expectancy(returns), 6),
        "max_drawdown": round(max_drawdown(returns), 6),
        "sharpe": round(sharpe(returns, periods_per_year), 4),
        "trade_count": len(returns),
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
    }
