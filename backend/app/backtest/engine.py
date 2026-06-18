"""Historical backtest runner.

Pulls the maximum daily history DXFeed provides for a symbol, builds a daily
return series, and reports metrics across 1D / 1W / 1M / YTD / full timeframes
plus an equity curve and monthly breakdown.

Degrades gracefully: if little (or no) history comes back, it returns what it can
and always surfaces the **actual date range covered** rather than pretending.

Rule-driven strategies replay their real entry/exit logic. A strategy that exposes
``backtest_daily_selection`` (the EMA scalper) is backtested the way it trades
live: each day the scanner picks the best stock and that one trade is taken
(``basis: "rule_driven_daily_selection"``). A rule-driven strategy without that
method falls back to a single-symbol replay, and a non-rule strategy to a
buy-and-hold baseline.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from app.backtest import metrics
from app.strategies.base import BaseStrategy

logger = logging.getLogger("trading.backtest")

# Far-past start so DXFeed returns the maximum history it has.
_MAX_LOOKBACK_START = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Approx trading-day counts for trailing windows.
_WINDOWS = {"1D": 1, "1W": 5, "1M": 21}


def _bar_date(bar: dict):
    ms = bar.get("time")
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _daily_returns(bars: list[dict]) -> list[tuple]:
    """(date, return) pairs from close-to-close. Skips bars without a close."""
    closes = [(d, b["close"]) for b in bars if (d := _bar_date(b)) and b["close"]]
    out = []
    for i in range(1, len(closes)):
        prev = closes[i - 1][1]
        cur = closes[i][1]
        if prev:
            out.append((closes[i][0], cur / prev - 1.0))
    return out


def _monthly_breakdown(dated_returns: list[tuple]) -> list[dict]:
    buckets: dict[str, list[float]] = {}
    for d, r in dated_returns:
        buckets.setdefault(f"{d.year}-{d.month:02d}", []).append(r)
    return [{"period": k, "return": round(metrics.total_return(v), 6)}
            for k, v in sorted(buckets.items())]


async def run_backtest(strategy: BaseStrategy, symbol: str | None = None,
                       overrides: dict | None = None, on_progress=None) -> dict:
    """Run the backtest for a strategy. ``symbol`` overrides the test instrument;
    ``overrides`` carries the strategy page's backtest-config controls.
    ``on_progress(scanned, total, symbol)`` (optional) streams universe-scan
    progress for the daily-selection path.

    Rule-driven strategies (those exposing ``backtest_returns``) replay their real
    entry/exit logic and produce per-trade returns; otherwise we fall back to the
    buy-and-hold baseline on the symbol."""
    if getattr(strategy, "is_rule_driven", False):
        return await _rule_driven_backtest(strategy, symbol, overrides, on_progress)
    return await _buy_and_hold_backtest(strategy, symbol)


def _params_from_overrides(strategy: BaseStrategy, overrides: dict):
    """Apply the backtest-config controls (rr / stop buffer / slope) onto the
    strategy's params, leaving the originals untouched."""
    from dataclasses import replace

    params = strategy.params
    pmap = {}
    if overrides.get("rr") is not None and hasattr(params, "risk_reward"):
        pmap["risk_reward"] = _f(overrides["rr"], params.risk_reward)
    # The scalper exposes stop_buffer_atr / slope_min; the crossover exposes
    # stop_atr_mult / vol_multiplier — only apply an override when the field exists
    # on this strategy's params.
    if overrides.get("stop_buffer") is not None and hasattr(params, "stop_buffer_atr"):
        pmap["stop_buffer_atr"] = _f(overrides["stop_buffer"], params.stop_buffer_atr)
    if overrides.get("slope_min") is not None and hasattr(params, "slope_min"):
        pmap["slope_min"] = _f(overrides["slope_min"], params.slope_min)
    if overrides.get("stop_atr_mult") is not None and hasattr(params, "stop_atr_mult"):
        pmap["stop_atr_mult"] = _f(overrides["stop_atr_mult"], params.stop_atr_mult)
    if overrides.get("vol_multiplier") is not None and hasattr(params, "vol_multiplier"):
        pmap["vol_multiplier"] = _f(overrides["vol_multiplier"], params.vol_multiplier)
    return replace(params, **pmap) if pmap else params


async def _rule_driven_backtest(strategy: BaseStrategy, symbol: str | None,
                                overrides: dict | None, on_progress=None) -> dict:
    """Backtest a strategy's actual rules, returning the rich result the strategy
    page renders (counts, $-metrics, trades, chart series). ``overrides`` carries
    the page's backtest-config controls.

    A multi-position strategy exposing ``backtest_portfolio`` is backtested the way
    it trades live (whole watchlist, one shared risk-sized pool). A strategy exposing
    ``backtest_daily_selection`` is backtested over the universe with daily scanner
    selection. Otherwise a single symbol is replayed."""
    overrides = overrides or {}
    params = _params_from_overrides(strategy, overrides)
    capital = _f(overrides.get("capital"), 2000.0)

    if getattr(strategy, "is_multi_position", False) and hasattr(strategy, "backtest_portfolio"):
        return await _portfolio_backtest(strategy, params, capital, symbol, overrides, on_progress)
    if hasattr(strategy, "backtest_daily_selection"):
        return await _daily_selection_backtest(strategy, params, capital, overrides, on_progress)

    target = symbol or overrides.get("symbol") or _default_symbol(strategy)
    start = _as_date(overrides.get("start"))
    end = _as_date(overrides.get("end"))

    result: dict = {
        "strategy_key": strategy.key, "symbol": target, "basis": "rule_driven",
        "data_available": False, "date_range": {"start": None, "end": None, "trading_days": 0},
        "config_used": {"symbol": target, "capital_per_trade": capital,
                        "risk_reward": getattr(params, "risk_reward", None),
                        "stop_buffer_atr": getattr(params, "stop_buffer_atr", None),
                        "slope_min": getattr(params, "slope_min", None),
                        "start": overrides.get("start"), "end": overrides.get("end")},
        "note": None,
    }
    try:
        sim = await strategy.backtest_returns(target, params)
    except Exception as exc:  # noqa: BLE001 — never 500 a backtest
        logger.warning("Rule-driven backtest failed: %s", exc)
        result["note"] = "Backtest data unavailable (auth/feed)."
        return result

    trades = sim.get("trades", [])
    if start or end:
        trades = [t for t in trades if _in_range(t.get("date"), start, end)]
    rng = sim["date_range"]
    result["date_range"] = {"start": rng["start"], "end": rng["end"],
                            "trading_days": rng.get("bars", 0)}
    result["note"] = sim.get("note")
    if not trades:
        return result

    result["data_available"] = True
    result.update(_summarize_trades(trades, capital))
    return result


async def _daily_selection_backtest(strategy: BaseStrategy, params, capital: float,
                                    overrides: dict, on_progress=None) -> dict:
    """Universe backtest with daily scanner selection — each day takes the single
    best-of-watchlist trade, exactly how the live worker rotates symbols."""
    from app.engine.universe import get_universe

    start_s, end_s = overrides.get("start"), overrides.get("end")
    result: dict = {
        "strategy_key": strategy.key, "symbol": "Universe (daily selection)",
        "basis": "rule_driven_daily_selection", "data_available": False,
        "date_range": {"start": None, "end": None, "trading_days": 0},
        "config_used": {"universe_daily_selection": True, "capital_per_trade": capital,
                        "risk_reward": params.risk_reward, "stop_buffer_atr": params.stop_buffer_atr,
                        "slope_min": params.slope_min, "start": start_s, "end": end_s},
        "note": None,
    }
    try:
        universe = await get_universe()
        sim = await strategy.backtest_daily_selection(universe, params, start_s, end_s,
                                                      on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 — never 500 a backtest
        logger.warning("Daily-selection backtest failed: %s", exc)
        result["note"] = "Backtest data unavailable (auth/feed)."
        return result

    result["date_range"] = sim["date_range"]
    result["universe_size"] = sim.get("universe_size")
    result["note"] = sim.get("note")
    trades = sim.get("trades", [])
    if not trades:
        return result

    result["data_available"] = True
    result.update(_summarize_trades(trades, capital, stocks_scanned=sim.get("universe_size", 0)))
    return result


async def _portfolio_backtest(strategy: BaseStrategy, params, capital: float,
                              symbol: str | None, overrides: dict, on_progress=None) -> dict:
    """Multi-position portfolio backtest — the whole watchlist run together through
    one shared risk-sized buying-power pool (how the crossover trades live). A
    ``symbol`` override narrows the run to a single name (watchlist-of-one) through
    the same simulator, so sizing stays risk-fractional. ``direction`` selects
    long / short / both."""
    from app.config import get_settings

    direction = (overrides.get("direction") or "both").lower()
    if direction not in ("long", "short", "both"):
        direction = "both"
    target = symbol or overrides.get("symbol")
    symbols = [target.upper()] if target else get_settings().watchlist_symbols
    start_s, end_s = overrides.get("start"), overrides.get("end")

    result: dict = {
        "strategy_key": strategy.key,
        "symbol": target.upper() if target else "Watchlist (portfolio)",
        "basis": "rule_driven_portfolio", "data_available": False,
        "date_range": {"start": None, "end": None, "trading_days": 0},
        "config_used": {"portfolio": True, "watchlist_size": len(symbols),
                        "direction": direction, "capital": capital,
                        "risk_reward": getattr(params, "risk_reward", None),
                        "start": start_s, "end": end_s},
        "note": None,
    }
    try:
        sim = await strategy.backtest_portfolio(
            symbols, params, direction=direction, start=start_s, end=end_s,
            capital=capital, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 — never 500 a backtest
        logger.warning("Portfolio backtest failed: %s", exc)
        result["note"] = "Backtest data unavailable (auth/feed)."
        return result

    result["date_range"] = sim["date_range"]
    result["universe_size"] = sim.get("universe_size")
    result["note"] = sim.get("note")
    trades = sim.get("trades", [])
    if not trades:
        return result

    result["data_available"] = True
    result.update(_summarize_portfolio(trades, capital, stocks_scanned=sim.get("universe_size", 1)))
    return result


def _summarize_portfolio(trades: list[dict], capital: float, stocks_scanned: int = 1) -> dict:
    """Counts, $-metrics and chart series for the portfolio backtest. Unlike
    ``_summarize_trades`` this consumes the **already-risk-sized** ``shares`` /
    ``pnl`` / ``capital_used`` on each trade (sizing parity with live) instead of
    recomputing ``capital // entry``. Adds ``avg_hold`` (minutes)."""
    returns = [t["return_pct"] for t in trades]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rmults = [t["r_multiple"] for t in trades]
    holds = [t["hold_minutes"] for t in trades if t.get("hold_minutes") is not None]
    gross_win, gross_loss = sum(wins), abs(sum(losses))

    summary = metrics.summarize(returns)
    net_profit = round(sum(pnls), 2)
    perf = {
        "net_profit": net_profit,
        "net_return_pct": round(net_profit / capital, 6) if capital else 0.0,
        "win_rate": summary["win_rate"],
        "sharpe": summary["sharpe"],
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "avg_r_multiple": round(sum(rmults) / len(rmults), 3) if rmults else 0.0,
        "avg_hold": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "max_drawdown": summary["max_drawdown"],
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "total_capital_used": round(sum(t["capital_used"] for t in trades), 2),
        "avg_capital_utilization": round(
            sum(t["capital_used"] for t in trades) / (len(trades) * capital), 4)
        if trades and capital else 0.0,
    }

    equity = [round(v, 2) for v in _cum(pnls, capital)]
    eq_dates = [trades[0]["date"]] + [t["date"] for t in trades]
    drawdown = _drawdown_series(equity)
    daily: dict[str, float] = {}
    monthly: dict[str, float] = {}
    for t in trades:
        daily[t["date"]] = round(daily.get(t["date"], 0.0) + t["pnl"], 2)
        monthly[(t["date"] or "")[:7]] = round(monthly.get((t["date"] or "")[:7], 0.0) + t["pnl"], 2)

    return {
        "results": {
            "stocks_scanned": stocks_scanned,
            "valid_setups": len(trades),
            "trades_taken": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
        },
        "metrics": perf,
        "trades": trades,
        "charts": {
            "equity_curve": [{"date": d, "equity": e} for d, e in zip(eq_dates, equity)],
            "daily_pnl": [{"date": d, "pnl": v} for d, v in sorted(daily.items())],
            "drawdown": [{"date": d, "drawdown": dd} for d, dd in zip(eq_dates, drawdown)],
            "monthly": [{"period": k, "pnl": v} for k, v in sorted(monthly.items())],
            "durations": [h for h in holds],
        },
    }


def _summarize_trades(trades: list[dict], capital: float, stocks_scanned: int = 1) -> dict:
    """Compute counts, $-metrics and chart series from detailed trades."""
    rows = []
    for t in trades:
        entry = t["entry_price"] or 0.0
        shares = int(capital // entry) if entry > 0 else 0
        direction = 1 if t["side"] == "long" else -1
        pnl = (t["exit_price"] - entry) * shares * direction
        rows.append({**t, "shares": shares, "capital_used": round(shares * entry, 2),
                     "pnl": round(pnl, 2)})

    returns = [t["return_pct"] for t in trades]
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rmults = [t["r_multiple"] for t in trades]
    gross_win, gross_loss = sum(wins), abs(sum(losses))

    summary = metrics.summarize(returns)
    net_profit = round(sum(pnls), 2)
    perf = {
        "net_profit": net_profit,
        "net_return_pct": round(net_profit / capital, 6) if capital else 0.0,
        "win_rate": summary["win_rate"],
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "avg_r_multiple": round(sum(rmults) / len(rmults), 3) if rmults else 0.0,
        "max_drawdown": summary["max_drawdown"],
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "total_capital_used": round(sum(r["capital_used"] for r in rows), 2),
        "avg_capital_utilization": round(
            sum(r["capital_used"] for r in rows) / (len(rows) * capital), 4) if rows and capital else 0.0,
    }

    # Chart series.
    equity = [round(v, 2) for v in _cum(pnls, capital)]
    eq_dates = [rows[0]["date"]] + [r["date"] for r in rows]
    drawdown = _drawdown_series(equity)
    daily: dict[str, float] = {}
    for r in rows:
        daily[r["date"]] = round(daily.get(r["date"], 0.0) + r["pnl"], 2)
    monthly: dict[str, float] = {}
    for r in rows:
        monthly[(r["date"] or "")[:7]] = round(monthly.get((r["date"] or "")[:7], 0.0) + r["pnl"], 2)
    durations = [_minutes(t["entry_time"], t["exit_time"]) for t in trades]

    return {
        "results": {
            "stocks_scanned": stocks_scanned,  # universe size (daily-selection) or 1 (single-symbol)
            "valid_setups": len(trades),
            "trades_taken": len(rows),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
        },
        "metrics": perf,
        "trades": rows,
        "charts": {
            "equity_curve": [{"date": d, "equity": e} for d, e in zip(eq_dates, equity)],
            "daily_pnl": [{"date": d, "pnl": v} for d, v in sorted(daily.items())],
            "drawdown": [{"date": d, "drawdown": dd} for d, dd in zip(eq_dates, drawdown)],
            "monthly": [{"period": k, "pnl": v} for k, v in sorted(monthly.items())],
            "durations": durations,
        },
    }


def _cum(pnls: list[float], start: float) -> list[float]:
    out = [start]
    for p in pnls:
        out.append(out[-1] + p)
    return out


def _drawdown_series(equity: list[float]) -> list[float]:
    peak = equity[0] if equity else 0.0
    out = []
    for v in equity:
        peak = max(peak, v)
        out.append(round(v - peak, 2))
    return out


def _minutes(start_iso: str | None, end_iso: str | None) -> float:
    if not start_iso or not end_iso:
        return 0.0
    a = datetime.fromisoformat(start_iso)
    b = datetime.fromisoformat(end_iso)
    return round((b - a).total_seconds() / 60.0, 1)


def _as_date(s):
    try:
        return date.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def _in_range(d: str | None, start, end) -> bool:
    if not d:
        return False
    dd = date.fromisoformat(d)
    return (start is None or dd >= start) and (end is None or dd <= end)


def _f(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_symbol(strategy: BaseStrategy) -> str:
    """Pick a representative liquid symbol for a no-arg backtest."""
    return "AAPL"


async def _buy_and_hold_backtest(strategy: BaseStrategy, symbol: str | None = None) -> dict:
    target = symbol or "SPY"

    result: dict = {
        "strategy_key": strategy.key,
        "symbol": target,
        "basis": "buy_and_hold_baseline",  # fallback for strategies without rule logic yet
        "data_available": False,
        "date_range": {"start": None, "end": None, "trading_days": 0},
        "timeframes": {},
        "equity_curve": [],
        "monthly_breakdown": [],
        "note": None,
    }

    try:
        from app.providers import get_historical_provider
        bars = await get_historical_provider().get_bars(target, "1d", start=_MAX_LOOKBACK_START)
    except Exception as exc:  # noqa: BLE001 — never 500 a backtest
        logger.warning("Backtest data fetch failed: %s", exc)
        result["note"] = "Historical data unavailable (auth/feed)."
        return result

    dated = _daily_returns(bars)
    if not dated:
        result["note"] = "No historical bars returned for this symbol/timeframe."
        return result

    returns = [r for _, r in dated]
    result["data_available"] = True
    result["date_range"] = {
        "start": dated[0][0].isoformat(),
        "end": dated[-1][0].isoformat(),
        "trading_days": len(dated),
    }

    # Timeframe metrics, degrading gracefully when a window exceeds available data.
    tfs: dict[str, dict] = {}
    for label, days in _WINDOWS.items():
        window = returns[-days:] if len(returns) >= 1 else []
        tfs[label] = metrics.summarize(window)
    this_year = datetime.now(timezone.utc).year
    ytd = [r for d, r in dated if d.year == this_year]
    tfs["YTD"] = metrics.summarize(ytd)
    tfs["full"] = metrics.summarize(returns)
    result["timeframes"] = tfs

    # Equity curve (downsample if very long to keep the payload light).
    curve_pts = metrics.equity_curve(returns)
    dates = [dated[0][0].isoformat()] + [d.isoformat() for d, _ in dated]
    step = max(1, len(curve_pts) // 500)
    result["equity_curve"] = [
        {"date": dates[i], "equity": round(curve_pts[i], 6)}
        for i in range(0, len(curve_pts), step)
    ]
    result["monthly_breakdown"] = _monthly_breakdown(dated)
    return result
