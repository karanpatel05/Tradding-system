"""Connors RSI-2 mean reversion — full OOS treatment. Standalone, report only.

Canonical config (pre-committed, no OOS tuning): long-only; enter when close > SMA200
AND RSI(2) < 10; exit when close > SMA5; no hard stop; entry/exit at close (MOC).
Equal-weight 10-sleeve portfolio (capital/N per name; cash when a sleeve is flat).

Reports per period (IS = older year, OOS = recent held-out year): daily-return portfolio
metrics, per-trade stats (the real test of the edge), market exposure, a buy&hold
benchmark, and a 5bp cost stress.

Run:  python -m scripts.run_rsi2 [--slip-bps 2]
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from app.config import get_settings
from app.services import history_cache
from app.strategies.daily_anomaly.rth_daily import to_rth_daily
from app.strategies.mean_reversion.rsi2 import simulate_rsi2
from scripts.run_daily_strategies import _leg_returns, _period, _profit_factor, _summary_returns

_SOURCE = "ibkr"


def _load_daily(symbol: str) -> list[dict]:
    entry = history_cache.load(symbol, "5m", source=_SOURCE)
    return to_rth_daily(entry.get("bars", [])) if entry else []


def _trade_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": None, "avg_win": 0.0,
                "avg_loss": 0.0, "avg_hold": 0.0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "profit_factor": _profit_factor(rets),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "avg_hold": round(sum(t["bars_held"] for t in trades) / len(trades), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Connors RSI-2 backtest with OOS treatment.")
    ap.add_argument("--slip-bps", type=float, default=2.0)
    args = ap.parse_args()
    symbols = get_settings().watchlist_symbols
    daily = {s: _load_daily(s) for s in symbols}
    daily = {s: d for s, d in daily.items() if d}
    n_sym = len(daily)
    all_dates = sorted({b["date"] for d in daily.values() for b in d})

    # Per symbol: run canonical RSI-2; collect daily MTM map, trades, holding days.
    sleeve_daily = {}      # symbol -> {date: ret}
    trades_by_period = {"IS": [], "OOS": []}
    sym_net = {"IS": defaultdict(float), "OOS": defaultdict(float)}
    hold_days = 0
    for s, d in daily.items():
        out = simulate_rsi2(d, slip_bps=args.slip_bps)
        sleeve_daily[s] = dict(out["daily"])
        hold_days += len(out["daily"])
        for t in out["trades"]:
            p = _period(t["entry_date"])
            trades_by_period[p].append(t)
            sym_net[p][s] += t["ret"]

    # Equal-weight portfolio daily return (flat sleeves contribute 0), split by period.
    port = {"IS": [], "OOS": []}
    for dt in all_dates:
        r = sum(sleeve_daily[s].get(dt, 0.0) for s in daily) / n_sym
        port[_period(dt)].append(r)

    capital = 2000.0
    exposure = hold_days / (n_sym * len(all_dates)) if all_dates else 0.0
    print(f"Connors RSI-2 | symbols={n_sym} | RTH daily | slip={args.slip_bps}bp "
          f"| equal-weight {n_sym} sleeves | market exposure {exposure:.1%}")
    print("  Config: long-only, close>SMA200 & RSI(2)<10 → buy MOC; exit close>SMA5; no stop.")
    print(f"  IS = older (<2025-06-05, tuning-equiv)   OOS = recent (>=2025-06-05, VERDICT)\n")

    # Portfolio (daily-return) metrics
    pm = {p: _summary_returns(port[p], capital=capital) for p in ("IS", "OOS")}
    ts = {p: _trade_stats(trades_by_period[p]) for p in ("IS", "OOS")}
    w = 16

    print("=== Portfolio (equal-weight, daily MTM) ===")
    print(f"  {'Metric':<16}{'IS (older)':>{w}}{'OOS (recent)':>{w}}")
    print(f"  {'-'*16}{'-'*w}{'-'*w}")
    for label, key, fmt in [("Net profit", "net_profit", "${:,.2f}"), ("Total return", "total_return", "{:.2%}"),
                            ("CAGR", "cagr", "{:.2%}"), ("Sharpe", "sharpe", "{:.3f}"),
                            ("Sortino", "sortino", "{:.3f}"), ("Profit factor", "profit_factor", "{:.3f}"),
                            ("Max drawdown", "max_drawdown", "{:.2%}")]:
        a, b = pm["IS"].get(key), pm["OOS"].get(key)
        fa = "n/a" if a is None else fmt.format(a)
        fb = "n/a" if b is None else fmt.format(b)
        print(f"  {label:<16}{fa:>{w}}{fb:>{w}}")
    print()

    print("=== Per-trade stats (the edge itself) ===")
    print(f"  {'Metric':<16}{'IS (older)':>{w}}{'OOS (recent)':>{w}}")
    print(f"  {'-'*16}{'-'*w}{'-'*w}")
    for label, key, fmt in [("Trades", "trades", "{:,}"), ("Win rate", "win_rate", "{:.1%}"),
                            ("Profit factor", "profit_factor", "{:.3f}"), ("Avg win", "avg_win", "{:.2%}"),
                            ("Avg loss", "avg_loss", "{:.2%}"), ("Avg hold (days)", "avg_hold", "{:.1f}")]:
        a, b = ts["IS"].get(key), ts["OOS"].get(key)
        fa = "n/a" if a is None else fmt.format(a)
        fb = "n/a" if b is None else fmt.format(b)
        print(f"  {label:<16}{fa:>{w}}{fb:>{w}}")
    pos_is = sum(1 for v in sym_net["IS"].values() if v > 0)
    pos_oos = sum(1 for v in sym_net["OOS"].values() if v > 0)
    print(f"  {'Symbols +':<16}{f'{pos_is}/{n_sym}':>{w}}{f'{pos_oos}/{n_sym}':>{w}}\n")

    # Benchmark: equal-weight buy&hold of the same names (gross).
    bh = {"IS": [], "OOS": []}
    for dt, r in _leg_returns(daily, "buyhold"):
        bh[_period(dt)].append(r)
    bhm = {p: _summary_returns(bh[p], capital=capital) for p in ("IS", "OOS")}
    print("=== Benchmark: buy & hold (equal-weight, gross) ===")
    print(f"  OOS: net ${bhm['OOS']['net_profit']:,.2f}  CAGR {bhm['OOS']['cagr']:.1%}  "
          f"Sharpe {bhm['OOS']['sharpe']:.3f}  maxDD {bhm['OOS']['max_drawdown']:.1%}\n")

    # Verdict (balanced robustness, judged on OOS).
    oos = pm["OOS"]
    pf = ts["OOS"]["profit_factor"]
    ok = (oos["net_profit"] > 0 and pos_oos > n_sym / 2 and pf is not None and pf >= 1.2)
    print("VERDICT — balanced robustness, judged on OOS:")
    print(f"  OOS net ${oos['net_profit']:,.2f} | CAGR {oos['cagr']:.1%} | Sharpe {oos['sharpe']:.3f} "
          f"| trade PF {('n/a' if pf is None else f'{pf:.2f}')} | sym+ {pos_oos}/{n_sym} | maxDD {oos['max_drawdown']:.1%}")
    print(f"  vs buy&hold CAGR {bhm['OOS']['cagr']:.1%} / Sharpe {bhm['OOS']['sharpe']:.3f}")
    print(f"  → {'DEPLOYABLE' if ok else 'NO'} (need OOS net>0, PF>=1.2, majority symbols +)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
