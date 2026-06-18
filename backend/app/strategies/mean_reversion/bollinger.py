"""Bollinger-style intraday mean reversion (long + short).

Per the tested spec: on a fixed-period window compute the SMA and (population) standard
deviation of close. Go LONG when close falls more than ``num_std`` σ below the mean
(expecting reversion up); go SHORT when it rises more than ``num_std`` σ above (reversion
down). Exit when close returns to the mean (SMA). No stop is specified by the method.

Pure over a bar list (``{date, open, high, low, close}``). Rolling window is continuous over
the bars passed in (caller decides RTH-only vs all-hours). Entry/exit fill on the close
(computable near the bar close); slippage charged both sides. Optional ``eod_flat`` closes
any open position on the last bar of each date (intraday variant).
"""

from __future__ import annotations

import math


def rolling_sma_std(closes: list[float], window: int) -> tuple[list[float | None], list[float | None]]:
    sma: list[float | None] = [None] * len(closes)
    std: list[float | None] = [None] * len(closes)
    for i in range(window - 1, len(closes)):
        w = closes[i - window + 1:i + 1]
        m = sum(w) / window
        var = sum((x - m) ** 2 for x in w) / window  # population std (Bollinger convention)
        sma[i] = m
        std[i] = math.sqrt(var)
    return sma, std


def simulate_bollinger(bars: list[dict], *, window: int = 20, num_std: float = 1.5,
                       slip_bps: float = 2.0, eod_flat: bool = False,
                       mode: str = "reversion", trend_period: int = 0) -> list[dict]:
    """Run the strategy over one symbol's bars; return realized trades.

    ``mode='reversion'`` (default): fade the band — long below −σ, short above +σ.
    ``mode='breakout'``: the opposite — long above +σ, short below −σ (ride momentum).
    Both exit when close returns to the mean (long exits at/above SMA, short at/below).

    ``trend_period>0`` adds a higher-timeframe directional filter (a SMA over that many
    bars): only take longs when close is above the trend SMA and shorts when below it —
    i.e. fade only *with* the larger trend (drops counter-trend fades / the big losers)."""
    closes = [b["close"] for b in bars]
    sma, std = rolling_sma_std(closes, window)
    trend = rolling_sma_std(closes, trend_period)[0] if trend_period > 0 else None
    slip = slip_bps / 10_000.0
    trades: list[dict] = []
    side: str | None = None
    entry_idx = 0

    n = len(bars)
    for i in range(n):
        if sma[i] is None:
            continue
        upper = sma[i] + num_std * std[i]
        lower = sma[i] - num_std * std[i]
        last_of_day = i + 1 >= n or bars[i + 1]["date"] != bars[i]["date"]

        if side is None:
            below, above = closes[i] < lower, closes[i] > upper
            go_long = below if mode == "reversion" else above
            go_short = above if mode == "reversion" else below
            if trend is not None:
                if trend[i] is None:          # trend not warmed up yet → no trade
                    continue
                go_long = go_long and closes[i] > trend[i]
                go_short = go_short and closes[i] < trend[i]
            if go_long:
                side, entry_idx = "long", i
            elif go_short:
                side, entry_idx = "short", i
            continue

        # In a position: exit on reversion to the mean (not on the entry bar),
        # or force-flat at end of day if requested.
        revert = (side == "long" and closes[i] >= sma[i]) or (side == "short" and closes[i] <= sma[i])
        if i > entry_idx and (revert or (eod_flat and last_of_day)):
            trades.append(_mk_trade(bars, closes, entry_idx, i, side, slip,
                                    "mean" if revert else "eod_flat"))
            side = None

    if side is not None:  # never reverted by end of data
        trades.append(_mk_trade(bars, closes, entry_idx, n - 1, side, slip, "eod"))
    return trades


def _mk_trade(bars, closes, ei, xi, side, slip, reason) -> dict:
    entry, exit_ = closes[ei], closes[xi]
    if side == "long":
        ret = (exit_ * (1 - slip)) / (entry * (1 + slip)) - 1
    else:  # short
        ret = (entry * (1 - slip) - exit_ * (1 + slip)) / entry
    return {"side": side, "entry_date": bars[ei]["date"], "exit_date": bars[xi]["date"],
            "entry_price": entry, "exit_price": exit_, "ret": ret, "exit_reason": reason,
            "bars_held": xi - ei}
