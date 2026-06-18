"""Overnight-drift strategy: hold from RTH close to the next RTH open.

The 'overnight effect' is a long-documented US-equity anomaly (most of the historical
equity premium has accrued outside RTH). Per symbol we compute each night's return;
the portfolio layer equal-weights eligible symbols per night. An optional trailing-SMA
filter only holds a symbol when its close is above its N-day moving average (regime on).
"""

from __future__ import annotations

from collections import defaultdict


def overnight_returns(days: list[dict], *, slip_bps: float = 2.0, sma: int | None = None) -> list[dict]:
    """Per-night returns for one symbol: buy close[t], sell open[t+1], net of slippage.

    Each result is ``{"date": <exit/open day>, "ret": <fraction>}``. With ``sma`` set,
    a night is only taken when close[t] exceeds the trailing ``sma``-day average of
    closes ending at t (needs at least ``sma`` prior closes)."""
    slip = slip_bps / 10_000.0
    out = []
    closes = [d["close"] for d in days]
    for i in range(len(days) - 1):
        entry, exit_ = days[i]["close"], days[i + 1]["open"]
        if entry <= 0 or exit_ <= 0:
            continue
        if sma is not None:
            if i + 1 < sma:
                continue
            avg = sum(closes[i - sma + 1:i + 1]) / sma
            if entry <= avg:
                continue
        ret = (exit_ * (1 - slip)) / (entry * (1 + slip)) - 1
        out.append({"date": days[i + 1]["date"], "ret": ret})
    return out


def portfolio_returns(daily_by_symbol: dict[str, list[dict]], *, slip_bps: float = 2.0,
                      sma: int | None = None) -> list[tuple[str, float]]:
    """Equal-weight overnight portfolio: each date's return is the mean across the
    symbols that were eligible that night. Returns ``[(date, ret), ...]`` sorted by date."""
    by_date: dict[str, list[float]] = defaultdict(list)
    for _sym, days in daily_by_symbol.items():
        for r in overnight_returns(days, slip_bps=slip_bps, sma=sma):
            by_date[r["date"]].append(r["ret"])
    return [(d, sum(v) / len(v)) for d, v in sorted(by_date.items()) if v]
