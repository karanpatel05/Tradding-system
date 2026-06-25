"""Triple Calendar backtest: drive the shared core over a date range from a price
source, record per-trade results, and summarize (incl. modeled-IV regime buckets)."""
from __future__ import annotations

from datetime import date

from app.strategies.triple_calendar.iv import implied_vol
from app.strategies.triple_calendar.strategy import decide_entry, manage, position_value


def _entry_iv(source, params, pos) -> float | None:
    """Modeled IV of the ATM short call at entry (middle calendar)."""
    cal = pos.calendars[len(pos.calendars) // 2]
    px = source.option_close(params.underlying, cal.sell_expiry, cal.strike, "C", pos.entry_date)
    spot = source.underlying_close(params.underlying, pos.entry_date)
    if px is None or spot is None:
        return None
    T = max((cal.sell_expiry - pos.entry_date).days, 1) / 365.0
    return implied_vol(px, "C", spot, cal.strike, T, params.risk_free_rate, params.div_yield)


def run_backtest(source, params, start: date, end: date, vix=None) -> list[dict]:
    days = source.trading_days(start, end)
    trades: list[dict] = []
    pos = None
    entry_iv = None
    for day in days:
        if pos is None:
            pos = decide_entry(day, source, params, params.event_dates, vix)
            if pos is not None:
                entry_iv = _entry_iv(source, params, pos)
            continue
        action, info = manage(pos, day, source, params)
        if action == "exit":
            value = info.get("value") or position_value(source, pos, day) or pos.net_debit
            trades.append({
                "underlying": params.underlying, "entry_date": pos.entry_date.isoformat(),
                "exit_date": day.isoformat(), "reason": info["reason"],
                "net_debit": round(pos.net_debit, 4),
                "pnl_pct": round((value - pos.net_debit) / pos.net_debit, 4) if pos.net_debit else 0.0,
                "hold_days": (day - pos.entry_date).days,
                "calendars": len(pos.calendars), "entry_iv": entry_iv,
            })
            pos, entry_iv = None, None
    return trades


def _max_drawdown(pnls: list[float]) -> float:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in pnls:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return round(mdd, 4)


def _bucket(iv: float | None) -> str:
    if iv is None:
        return "unknown"
    return "low(<20%)" if iv < 0.20 else "mid(20-35%)" if iv < 0.35 else "high(>=35%)"


def compute_metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"total_trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
                "avg_hold_days": 0.0, "max_drawdown": 0.0, "iv_regime": {}}
    pnls = [t["pnl_pct"] for t in trades]
    wins = sum(1 for r in pnls if r > 0)
    regime: dict[str, list[float]] = {}
    for t in trades:
        regime.setdefault(_bucket(t.get("entry_iv")), []).append(t["pnl_pct"])
    iv_regime = {k: {"trades": len(v), "win_rate": round(sum(1 for r in v if r > 0) / len(v), 4),
                     "avg_pnl_pct": round(sum(v) / len(v), 4)} for k, v in regime.items()}
    return {
        "total_trades": n,
        "win_rate": round(wins / n, 4),
        "avg_pnl_pct": round(sum(pnls) / n, 4),
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / n, 1),
        "max_drawdown": _max_drawdown(pnls),
        "iv_regime": iv_regime,
    }
