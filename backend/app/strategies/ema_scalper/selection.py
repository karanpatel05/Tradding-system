"""Daily stock-selection history (Section 8).

Transparency layer over the universe backtest: for each trading day it reports how
many stocks were scanned, how many passed the scanner, which produced valid intraday
setups, which one was *selected* (highest scanner score) and the trade outcome — with
the full list of that day's setups for drill-down.

The heavy lifting (per-symbol intraday replay through the shared ``generate_signal``)
is done by the strategy; the functions here are pure aggregation so they're unit-
testable without any data.
"""

from __future__ import annotations

from math import floor

from app.strategies.ema_scalper import indicators as ind
from app.strategies.ema_scalper.params import EmaScalperParams
from app.strategies.ema_scalper.scanner import _RVOL_WINDOW, score_symbol


def pass_days(daily_bars: list[dict], params: EmaScalperParams) -> dict[str, float]:
    """For each day with enough history, the scanner score if the symbol passes
    every filter as-of that day (keyed by ISO date). No-lookahead: each day is
    scored from daily bars up to and including that day only."""
    from datetime import datetime, timezone

    need = max(params.trend_ema_length, params.atr_length, _RVOL_WINDOW) + 2
    out: dict[str, float] = {}
    for i in range(need, len(daily_bars)):
        cand = score_symbol(daily_bars[: i + 1], params)
        if cand is None:
            continue
        ms = daily_bars[i].get("time")
        if not ms:
            continue
        day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
        out[day] = cand.score
    return out


def _trade_pnl(trade: dict, capital: float) -> tuple[int, float]:
    entry = trade.get("entry_price") or 0.0
    if entry <= 0 or entry > capital:
        return 0, 0.0
    shares = int(floor(capital / entry))
    direction = 1 if trade.get("side") == "long" else -1
    pnl = (trade["exit_price"] - entry) * shares * direction
    return shares, round(pnl, 2)


def select_daily_trades(analyses: list[dict], start: str | None = None,
                        end: str | None = None) -> list[dict]:
    """Best-of-watchlist daily selection for the universe backtest.

    For each trading day (oldest→newest), pick the highest-scoring symbol that
    both passed the scanner *and* formed a setup that day, and return that
    symbol's full trade dict. This is the same "selected" stock Section 8 reports
    (see :func:`aggregate_selection`), so the backtest and the selection history
    agree day-for-day. At most one trade per day — mirroring the live worker,
    which holds a single selected symbol at a time.
    """
    days: set[str] = set()
    for a in analyses:
        days |= set(a["pass_days"].keys())

    chosen: list[dict] = []
    for day in sorted(days):
        if start and day < start:
            continue
        if end and day > end:
            continue
        best: tuple[float, dict] | None = None
        for a in analyses:
            score = a["pass_days"].get(day)
            if score is None:
                continue
            trade = a["trades_by_day"].get(day)
            if trade is None:
                continue
            if best is None or score > best[0]:
                best = (score, trade)
        if best is not None:
            chosen.append(best[1])
    return chosen


def aggregate_selection(universe_size: int, analyses: list[dict], capital: float,
                        start: str | None = None, end: str | None = None) -> list[dict]:
    """Build per-day selection rows from per-symbol analyses.

    Each analysis: ``{"symbol", "pass_days": {day: score}, "trades_by_day": {day: trade}}``.
    Returns rows newest-first; each row carries the ranked ``setups`` for expansion."""
    days: set[str] = set()
    for a in analyses:
        days |= set(a["pass_days"].keys())

    rows: list[dict] = []
    for day in sorted(days):
        if start and day < start:
            continue
        if end and day > end:
            continue
        passing = 0
        setups: list[dict] = []
        for a in analyses:
            score = a["pass_days"].get(day)
            if score is None:
                continue
            passing += 1
            trade = a["trades_by_day"].get(day)
            if trade:
                shares, pnl = _trade_pnl(trade, capital)
                setups.append({
                    "symbol": a["symbol"], "score": score, "side": trade["side"],
                    "entry_type": trade["entry_type"], "shares": shares, "pnl": pnl,
                    "r_multiple": trade.get("r_multiple"),
                    "outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT",
                })
        setups.sort(key=lambda s: s["score"], reverse=True)
        sel = setups[0] if setups else None
        rows.append({
            "date": day,
            "stocks_scanned": universe_size,
            "stocks_passing": passing,
            "valid_setups": len(setups),
            "selected": sel["symbol"] if sel else None,
            "scanner_score": sel["score"] if sel else None,
            "entry_type": sel["entry_type"] if sel else None,
            "outcome": sel["outcome"] if sel else None,
            "pnl": sel["pnl"] if sel else None,
            "selection_reason": "Highest Scanner Score" if sel else None,
            "setups": setups,
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows
