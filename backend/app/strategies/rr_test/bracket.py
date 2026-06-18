"""Fixed risk-reward bracket harness for the 1:3 win-rate study.

Bolts an identical bracket onto different long entry signals: stop = entry − atr_mult×ATR,
target = entry + rr×(atr_mult×ATR). Resolves on daily OHLC, stop wins ties (pessimistic).
Used to measure the *real* win rate each entry achieves at a 3:1 target — the high win rate
+ high RR combination people ask for almost never survives contact with data.

Long-only. Entry fills at the next bar's open (no look-ahead: signal is confirmed on the
prior close). Slippage charged on entry and on market exits (stop / EOD); the target is a
resting limit and fills free.
"""

from __future__ import annotations

from app.strategies.daily_anomaly.donchian import atr
from app.strategies.mean_reversion.rsi2 import rsi, sma


def ema(values: list[float], period: int) -> list[float]:
    m = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * m + out[-1] * (1 - m))
    return out


def resolve_bracket(bars: list[dict], start_j: int, *, entry_fill: float, R: float,
                    slip: float) -> tuple[float, str, float]:
    """Walk forward from bar ``start_j``; return (exit_price, reason, pnl_in_R).
    Reason ∈ {target, stop, eod}; stop wins ties."""
    stop = entry_fill - R
    target = entry_fill + 3.0 * R  # rr applied by caller via R; 3:1 hard-set here
    for j in range(start_j, len(bars)):
        b = bars[j]
        if b["low"] <= stop:
            return stop, "stop", -1.0
        if b["high"] >= target:
            return target, "target", 3.0
    exit_fill = bars[-1]["close"] * (1 - slip)
    return exit_fill, "eod", (exit_fill - entry_fill) / R


# ---- entry signal generators (return indices i; enter at open[i+1]) ----

def ema_cross_signals(closes: list[float], fast: int = 9, slow: int = 15) -> list[int]:
    ef, es = ema(closes, fast), ema(closes, slow)
    return [i for i in range(slow, len(closes) - 1)
            if ef[i] > es[i] and ef[i - 1] <= es[i - 1]]


def rsi2_signals(bars: list[dict]) -> list[int]:
    closes = [b["close"] for b in bars]
    r, trend = rsi(closes, 2), sma(closes, 200)
    return [i for i in range(len(bars) - 1)
            if r[i] is not None and trend[i] is not None and closes[i] > trend[i] and r[i] < 10]


def donchian_signals(bars: list[dict], n: int = 20) -> list[int]:
    return [i for i in range(n, len(bars) - 1)
            if bars[i]["close"] > max(b["high"] for b in bars[i - n:i])]


def run_bracket(bars: list[dict], signal_idxs: list[int], *, atr_mult: float = 1.0,
                slip_bps: float = 2.0) -> list[dict]:
    """Take each non-overlapping signal, enter next open, resolve the 1:3 ATR bracket."""
    a = atr(bars, period=14)
    slip = slip_bps / 10_000.0
    sigset = set(signal_idxs)
    trades: list[dict] = []
    i, nbars = 0, len(bars)
    while i < nbars - 1:
        if i in sigset and a[i] > 0:
            entry_fill = bars[i + 1]["open"] * (1 + slip)
            R = atr_mult * a[i]
            if R > 0 and entry_fill > 0:
                exit_price, reason, pnl_r = resolve_bracket(
                    bars, i + 1, entry_fill=entry_fill, R=R, slip=slip)
                # find exit index to advance past the trade (no overlapping positions)
                exit_idx = _exit_index(bars, i + 1, entry_fill, R)
                trades.append({"entry_date": bars[i + 1]["date"], "exit_reason": reason,
                               "pnl_r": pnl_r, "R": R, "entry_fill": entry_fill})
                i = max(exit_idx, i + 1)
                continue
        i += 1
    return trades


def _exit_index(bars, start_j, entry_fill, R):
    stop, target = entry_fill - R, entry_fill + 3.0 * R
    for j in range(start_j, len(bars)):
        if bars[j]["low"] <= stop or bars[j]["high"] >= target:
            return j
    return len(bars) - 1
