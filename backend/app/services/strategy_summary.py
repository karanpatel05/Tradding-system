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


FINISHED = ("settled", "closed")
# Entries that never became a fly but may still carry P&L: an unwind of a partial
# entry books its gross P&L and fees on the session row only (no TradeRecord).
NO_FLY = ("skipped", "failed")


def _counts_as_session(s: dict) -> bool:
    pnl = s.get("realized_pnl")
    if pnl is None:
        return False
    return s.get("status") in FINISHED or (s.get("status") in NO_FLY and round(float(pnl), 2) != 0)


def session_record(sessions: list[dict]) -> dict | None:
    """Record from a strategy's own session rows (e.g. the XSP fly store), keyed by
    session_date and using each row's realized_pnl, which includes unwind P&L and
    fees that never reach a TradeRecord. Settled/closed sessions count, as do
    skipped/failed days with a nonzero realized P&L (an unwind loss); other skipped,
    missed, failed, killed and errored days are reported as counts. None when there
    are no rows."""
    if not sessions:
        return None
    done = sorted((s for s in sessions if _counts_as_session(s)), key=lambda s: str(s.get("session_date")))
    pnls = [float(s["realized_pnl"]) for s in done]
    peak = equity = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    wins = sum(1 for p in pnls if p > 0)
    count = lambda *st: sum(1 for s in sessions if s.get("status") in st)
    dates = [str(s.get("session_date")) for s in done]
    return {
        "source": "sessions",
        "sessions": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 4) if pnls else None,
        "net_pnl": round(sum(pnls), 2),
        "max_drawdown": round(max_dd, 2),
        "skipped": sum(1 for s in sessions if s.get("status") in ("skipped", "missed", "failed") and not _counts_as_session(s)),
        "killed": count("killed", "error"),
        "open": count("open", "entering"),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
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
