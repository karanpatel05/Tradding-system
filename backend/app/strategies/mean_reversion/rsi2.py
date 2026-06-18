"""Connors RSI-2 mean reversion (canonical, long-only).

Larry Connors' short-term reversion: in an uptrend (close > 200-day SMA), buy when the
2-period RSI is deeply oversold (< 10) and exit when price closes back above its 5-day
SMA. No hard stop — that is intrinsic to the method (a stop turns the edge negative).
Entry and exit fire on the close (computed near the bell, filled MOC).

Pure functions over daily bars (``{date, open, high, low, close}``). Slippage is charged
on the entry buy and exit sell. Produces both discrete trades and a daily mark-to-market
return series so the portfolio layer can build an equity curve comparable to other strats.
"""

from __future__ import annotations


def sma(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(closes)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def rsi(closes: list[float], period: int) -> list[float | None]:
    """Wilder RSI, aligned to ``closes``; None before ``period`` deltas exist."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def simulate_rsi2(bars: list[dict], *, rsi_period: int = 2, entry: float = 10.0,
                  exit_sma: int = 5, trend_period: int = 200, slip_bps: float = 2.0) -> dict:
    """Run canonical RSI-2 over one symbol's daily bars.

    Returns ``{"trades": [...], "daily": [(date, ret), ...]}`` where ``daily`` is the
    mark-to-market return on the days a position is held (entry buy / exit sell slippage
    folded into the first / last day)."""
    closes = [b["close"] for b in bars]
    r = rsi(closes, rsi_period)
    trend = sma(closes, trend_period)
    ex = sma(closes, exit_sma)
    slip = slip_bps / 10_000.0

    trades: list[dict] = []
    daily: list[tuple[str, float]] = []
    entry_idx: int | None = None

    for i in range(len(bars)):
        if entry_idx is None:
            if (r[i] is not None and trend[i] is not None
                    and closes[i] > trend[i] and r[i] < entry):
                entry_idx = i
            continue
        # In a position: exit when close back above the short SMA (not on entry bar).
        if i > entry_idx and ex[i] is not None and closes[i] > ex[i]:
            _record(trades, daily, bars, closes, entry_idx, i, "sma_exit", slip)
            entry_idx = None

    if entry_idx is not None:  # still open at end of data
        _record(trades, daily, bars, closes, entry_idx, len(bars) - 1, "eod", slip)
    return {"trades": trades, "daily": daily}


def _record(trades, daily, bars, closes, ei, xi, reason, slip):
    entry_fill = closes[ei] * (1 + slip)
    exit_fill = closes[xi] * (1 - slip)
    # Daily mark-to-market for each held day (ei, xi]; fold slippage into first/last.
    for d in range(ei + 1, xi + 1):
        prev = entry_fill if d == ei + 1 else closes[d - 1]
        cur = exit_fill if d == xi else closes[d]
        if prev > 0:
            daily.append((bars[d]["date"], cur / prev - 1))
    trades.append({
        "entry_date": bars[ei]["date"], "exit_date": bars[xi]["date"],
        "entry_price": closes[ei], "exit_price": closes[xi], "exit_reason": reason,
        "ret": exit_fill / entry_fill - 1 if entry_fill > 0 else 0.0,
        "bars_held": xi - ei,
    })
