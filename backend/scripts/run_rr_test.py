"""1:3 risk-reward win-rate study. Standalone, report only.

Bolts an identical 1×ATR-stop / 3×ATR-target bracket onto three long entry signals
(EMA 9/15 cross, RSI-2 oversold, Donchian-20 breakout), across the 10 names, and measures
the REAL win rate each achieves at a 3:1 target. Question on the table: can any of them
deliver ~50% win at 1:3 (≈ +1R/trade)? Breakeven win rate at 1:3 is 25%.

Run:  python -m scripts.run_rr_test
"""

from __future__ import annotations

from collections import defaultdict

from app.config import get_settings
from app.services import history_cache
from app.strategies.daily_anomaly.rth_daily import to_rth_daily
from app.strategies.rr_test.bracket import (donchian_signals, ema_cross_signals,
                                            rsi2_signals, run_bracket)
from scripts.run_daily_strategies import _period

_SOURCE = "ibkr"


def _load_daily(symbol: str) -> list[dict]:
    entry = history_cache.load(symbol, "5m", source=_SOURCE)
    return to_rth_daily(entry.get("bars", [])) if entry else []


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    rs = [t["pnl_r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["exit_reason"]] += 1
    pos, neg = sum(wins), abs(sum(losses))
    return {
        "n": n,
        "win_rate": len([r for r in rs if r > 0]) / n,
        "target_pct": reasons["target"] / n,
        "stop_pct": reasons["stop"] / n,
        "eod_pct": reasons["eod"] / n,
        "expectancy_r": sum(rs) / n,
        "avg_win_r": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": pos / neg if neg > 0 else None,
    }


def main() -> int:
    symbols = get_settings().watchlist_symbols
    daily = {s: _load_daily(s) for s in symbols}
    daily = {s: d for s, d in daily.items() if d}

    generators = {
        "EMA 9/15 cross (momentum)": lambda bars: ema_cross_signals([b["close"] for b in bars]),
        "RSI-2 oversold (reversion)": rsi2_signals,
        "Donchian-20 breakout": donchian_signals,
    }

    print(f"1:3 R:R win-rate study | symbols={len(daily)} | long-only | "
          f"stop=1×ATR target=3×ATR | 2bp/side")
    print("  Breakeven win rate at 1:3 = 25%.  Target asked: 50% win.\n")

    w = 14
    cols = ("N", "Win%", "Target%", "Stop%", "Exp(R)", "AvgW(R)", "AvgL(R)", "PF")
    for name, gen in generators.items():
        by_period = {"IS": [], "OOS": []}
        for s, d in daily.items():
            for t in run_bracket(d, gen(d)):
                by_period[_period(t["entry_date"])].append(t)
        print(f"=== {name} ===")
        print("  " + f"{'period':<8}" + "".join(f"{c:>{w}}" for c in cols))
        print("  " + "-" * (8 + w * len(cols)))
        for p in ("IS", "OOS"):
            m = _stats(by_period[p])
            if m["n"] == 0:
                print(f"  {p:<8}{'(no trades)':>{w}}")
                continue
            pf = "n/a" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
            print("  " + f"{p:<8}{m['n']:>{w}}{m['win_rate']:>{w-1}.1%} {m['target_pct']:>{w-1}.1%} "
                  f"{m['stop_pct']:>{w-1}.1%} {m['expectancy_r']:>{w}.3f}{m['avg_win_r']:>{w}.2f}"
                  f"{m['avg_loss_r']:>{w}.2f}{pf:>{w}}")
        print()

    print("Read: at a fixed 3:1 target, win rate = the % of entries that travel +3R before −1R.")
    print("Expectancy is in R (risk units): >0 means the entry has an edge at this bracket.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
