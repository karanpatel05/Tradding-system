"""Per-strategy summaries for the Strategies screen: today's realized P&L and a
research snapshot. Pure functions over trade rows / stored backtests so they can be
tested without a database. All numbers come from the app's own records; nothing here
fabricates or samples data.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def et_date(dt: datetime | None) -> date | None:
    """New York calendar date of a timestamp (naive timestamps are treated as UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date()


def today_et(now: datetime | None = None) -> date:
    return et_date(now or datetime.now(timezone.utc))


def session_pnl(trades, today: date) -> float:
    """Realized P&L of closed trades whose exit falls on `today` (New York date)."""
    return round(sum(t.pnl or 0.0 for t in trades
                     if t.status == "closed" and t.pnl is not None and et_date(t.exit_time) == today), 2)


def paper_record(trades) -> dict | None:
    """Session-level record from closed trades, grouped by New York exit date.
    Both live strategies trade at most one position per day, so a session is the
    natural unit (an iron fly's legs are several trade rows but one session).
    Returns None when there is no closed trade yet."""
    daily: dict[date, float] = defaultdict(float)
    for t in trades:
        d = et_date(t.exit_time)
        if t.status == "closed" and t.pnl is not None and d is not None:
            daily[d] += t.pnl
    if not daily:
        return None
    days = sorted(daily)
    pnls = [round(daily[d], 2) for d in days]
    peak = equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "source": "paper",
        "sessions": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 4),
        "net_pnl": round(sum(pnls), 2),
        "max_drawdown": round(max_dd, 2),  # dollars, <= 0
        "first_date": days[0].isoformat(),
        "last_date": days[-1].isoformat(),
    }


def backtest_snapshot(result: dict | None) -> dict | None:
    """Snapshot from a stored backtest result (the longest timeframe it has)."""
    if not result or not result.get("timeframes"):
        return None
    tfs = result["timeframes"]
    for tf in ("full", "YTD", "1M", "1W", "1D"):
        m = tfs.get(tf)
        if m:
            rng = result.get("date_range") or {}
            return {
                "source": "backtest",
                "timeframe": tf,
                "trades": m.get("trade_count"),
                "win_rate": m.get("win_rate"),
                "max_drawdown_pct": m.get("max_drawdown"),  # fraction, <= 0
                "total_return_pct": m.get("total_return"),
                "symbol": result.get("symbol"),
                "first_date": rng.get("start"),
                "last_date": rng.get("end"),
            }
    return None
