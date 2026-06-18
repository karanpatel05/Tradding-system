"""Backtest + OOS verdict for the daily-anomaly strategies (A: overnight drift,
B: Donchian breakout). Standalone — reads data_cache/, reports only, not wired live.

Convention (corrected vs the ORB spec): tune on the OLDER year (in-sample), judge the
verdict on the most-recent held-out year (out-of-sample). Each strategy runs over the
full per-symbol history and results are bucketed into IS/OOS by date, preserving
indicator warmup across the boundary.

Run:  python -m scripts.run_daily_strategies
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict

from app.config import get_settings
from app.services import history_cache
from app.strategies.daily_anomaly.donchian import simulate_donchian
from app.strategies.daily_anomaly.overnight import overnight_returns, portfolio_returns
from app.strategies.daily_anomaly.rth_daily import to_rth_daily

_SOURCE = "massive"
_BOUNDARY = "2025-06-05"
_N_GRID = [10, 20, 55]
_K_GRID = [1.5, 2.0, 3.0]


def _load_daily(symbol: str) -> list[dict]:
    entry = history_cache.load(symbol, "5m", source=_SOURCE)
    return to_rth_daily(entry.get("bars", [])) if entry else []


def _period(date: str) -> str:
    return "OOS" if date >= _BOUNDARY else "IS"


def _sortino(rets: list[float], ppy: int) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    downside = [r for r in rets if r < 0]
    dvar = sum(r * r for r in downside) / len(rets) if downside else 0.0
    dsd = math.sqrt(dvar)
    return round(mean / dsd * math.sqrt(ppy), 3) if dsd > 0 else 0.0


def _profit_factor(vals: list[float]):
    gains = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    return round(gains / losses, 3) if losses > 0 else None


def _summary_returns(rets: list[float], *, capital: float, ppy: int = 252) -> dict:
    """Metric set for a return-series strategy (overnight). $ from compounding capital."""
    if not rets:
        return {"net_profit": 0.0, "total_return": 0.0, "cagr": 0.0, "sharpe": 0.0,
                "sortino": 0.0, "profit_factor": None, "win_rate": 0.0, "max_drawdown": 0.0,
                "periods": 0}
    n = len(rets)
    eq, curve = 1.0, [1.0]
    for r in rets:
        eq *= (1 + r)
        curve.append(eq)
    total = eq - 1
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    sharpe = round(mean / sd * math.sqrt(ppy), 3) if sd > 0 else 0.0
    return {
        "net_profit": round(capital * total, 2),
        "total_return": round(total, 4),
        "cagr": round((eq ** (ppy / n) - 1), 4) if eq > 0 else -1.0,
        "sharpe": sharpe,
        "sortino": _sortino(rets, ppy),
        "profit_factor": _profit_factor(rets),
        "win_rate": round(sum(1 for r in rets if r > 0) / n, 4),
        "max_drawdown": round(mdd, 4),
        "periods": n,
    }


def _summary_trades(trades: list[dict], *, capital: float) -> dict:
    """Metric set for a trade-list strategy (Donchian). $ = sum of per-trade net."""
    if not trades:
        return {"net_profit": 0.0, "total_trades": 0, "win_rate": 0.0, "profit_factor": None,
                "avg_win": 0.0, "avg_loss": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "sortino": 0.0}
    nets = [t["net"] for t in trades]
    rets = [t["ret"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    eq, curve = capital, [capital]
    for x in nets:
        eq += x
        curve.append(eq)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {
        "net_profit": round(sum(nets), 2),
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "profit_factor": _profit_factor(nets),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "max_drawdown": round(mdd, 4),
        "sharpe": round((sum(rets) / len(rets)) / (_std(rets) or 1) * math.sqrt(252), 3) if _std(rets) else 0.0,
        "sortino": _sortino(rets, 252),
    }


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _pct_symbols_positive(per_symbol_net: dict[str, float]) -> str:
    if not per_symbol_net:
        return "0/0"
    pos = sum(1 for v in per_symbol_net.values() if v > 0)
    return f"{pos}/{len(per_symbol_net)}"


# ---------- Strategy A: overnight drift ----------

def run_overnight(daily: dict[str, list[dict]], *, slip_bps: float, capital: float, sma: int | None):
    by_period_port: dict[str, list[float]] = {"IS": [], "OOS": []}
    for date, ret in portfolio_returns(daily, slip_bps=slip_bps, sma=sma):
        by_period_port[_period(date)].append(ret)
    # per-symbol net (compounded) per period, for %-positive
    sym_net = {"IS": {}, "OOS": {}}
    for sym, days in daily.items():
        acc = {"IS": 1.0, "OOS": 1.0}
        for r in overnight_returns(days, slip_bps=slip_bps, sma=sma):
            acc[_period(r["date"])] *= (1 + r["ret"])
        for p in ("IS", "OOS"):
            sym_net[p][sym] = capital * (acc[p] - 1)
    return {p: _summary_returns(by_period_port[p], capital=capital) for p in ("IS", "OOS")}, sym_net


# ---------- Strategy B: Donchian breakout ----------

def run_donchian(daily: dict[str, list[dict]], *, n: int, k: float, slip_bps: float, capital: float):
    by_period: dict[str, list[dict]] = {"IS": [], "OOS": []}
    sym_net = {"IS": defaultdict(float), "OOS": defaultdict(float)}
    for sym, days in daily.items():
        for t in simulate_donchian(days, n=n, k=k, capital=capital, slip_bps=slip_bps):
            p = _period(t["entry_date"])
            by_period[p].append(t)
            sym_net[p][sym] += t["net"]
    return ({p: _summary_trades(by_period[p], capital=capital) for p in ("IS", "OOS")},
            {p: dict(sym_net[p]) for p in ("IS", "OOS")})


def _leg_returns(daily: dict[str, list[dict]], leg: str) -> list[tuple[str, float]]:
    """Equal-weight daily portfolio returns for one leg of the day, GROSS of cost
    (benchmark attribution): overnight = close[t-1]->open[t]; intraday = open[t]->close[t];
    buyhold = close[t-1]->close[t] (= overnight compounded with intraday)."""
    by_date: dict[str, list[float]] = defaultdict(list)
    for _sym, days in daily.items():
        for i in range(1, len(days)):
            d, p = days[i], days[i - 1]
            if p["close"] <= 0 or d["open"] <= 0:
                continue
            if leg == "overnight":
                r = d["open"] / p["close"] - 1
            elif leg == "intraday":
                r = d["close"] / d["open"] - 1
            else:
                r = d["close"] / p["close"] - 1
            by_date[d["date"]].append(r)
    return [(dt, sum(v) / len(v)) for dt, v in sorted(by_date.items()) if v]


def run_benchmark(daily: dict[str, list[dict]], *, capital: float) -> dict[str, dict]:
    """GROSS overnight / intraday / buy&hold equal-weight portfolio metrics per period."""
    out: dict[str, dict] = {}
    for leg in ("overnight", "intraday", "buyhold"):
        per = {"IS": [], "OOS": []}
        for date, r in _leg_returns(daily, leg):
            per[_period(date)].append(r)
        out[leg] = {p: _summary_returns(per[p], capital=capital) for p in ("IS", "OOS")}
    return out


def _print_block(title: str, is_m: dict, oos_m: dict, keys, is_pos="", oos_pos=""):
    print(f"=== {title} ===")
    w = 16
    print(f"  {'Metric':<16}{'IS (older)':>{w}}{'OOS (recent)':>{w}}")
    print(f"  {'-'*16}{'-'*w}{'-'*w}")
    for label, key, fmt in keys:
        a = is_m.get(key)
        b = oos_m.get(key)
        fa = "n/a" if a is None else fmt.format(a)
        fb = "n/a" if b is None else fmt.format(b)
        print(f"  {label:<16}{fa:>{w}}{fb:>{w}}")
    if is_pos or oos_pos:
        print(f"  {'Symbols +':<16}{is_pos:>{w}}{oos_pos:>{w}}")
    print()


_RET_KEYS = [("Net profit", "net_profit", "${:,.2f}"), ("Total return", "total_return", "{:.2%}"),
             ("CAGR", "cagr", "{:.2%}"), ("Sharpe", "sharpe", "{:.3f}"),
             ("Sortino", "sortino", "{:.3f}"), ("Profit factor", "profit_factor", "{:.3f}"),
             ("Win rate", "win_rate", "{:.1%}"), ("Max drawdown", "max_drawdown", "{:.2%}"),
             ("Periods (days)", "periods", "{:,}")]
_TRADE_KEYS = [("Net profit", "net_profit", "${:,.2f}"), ("Total trades", "total_trades", "{:,}"),
               ("Win rate", "win_rate", "{:.1%}"), ("Profit factor", "profit_factor", "{:.3f}"),
               ("Avg win", "avg_win", "${:,.2f}"), ("Avg loss", "avg_loss", "${:,.2f}"),
               ("Sharpe", "sharpe", "{:.3f}"), ("Sortino", "sortino", "{:.3f}"),
               ("Max drawdown", "max_drawdown", "{:.2%}")]


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily-anomaly strategies backtest + OOS verdict.")
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--capital", type=float, default=2000.0)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    args = ap.parse_args()
    symbols = [s.upper() for s in args.symbols] or get_settings().watchlist_symbols

    daily = {}
    missing = []
    for sym in symbols:
        d = _load_daily(sym)
        (daily.__setitem__(sym, d) if d else missing.append(sym))
    dates = [b["date"] for d in daily.values() for b in d]
    print(f"Daily-anomaly backtest | symbols={len(daily)} | RTH daily from 5m "
          f"| range {min(dates)}→{max(dates)} | capital=${args.capital:,.0f} slip={args.slip_bps}bp")
    if missing:
        print(f"  (no cache: {', '.join(missing)})")
    print(f"  IS  = older  (date <  {_BOUNDARY})  ← tuning")
    print(f"  OOS = recent (date >= {_BOUNDARY})  ← VERDICT\n")

    verdicts = {}

    # --- A: overnight drift, two variants ---
    for label, sma in [("A1 Overnight (always-on)", None), ("A2 Overnight (close>SMA50)", 50)]:
        m, sym_net = run_overnight(daily, slip_bps=args.slip_bps, capital=args.capital, sma=sma)
        _print_block(label, m["IS"], m["OOS"], _RET_KEYS,
                     _pct_symbols_positive(sym_net["IS"]), _pct_symbols_positive(sym_net["OOS"]))
        verdicts[label] = (m["OOS"], _pct_symbols_positive(sym_net["OOS"]))

    # --- Benchmark: gross overnight/intraday/buyhold of the same 10 names ---
    bench = run_benchmark(daily, capital=args.capital)
    print("=== BENCHMARK (same 10 names, equal-weight, GROSS of cost) ===")
    w = 14
    print(f"  {'Leg':<14}{'period':<5}{'Net $':>{w}}{'TotRet':>{w}}{'Sharpe':>{w}}"
          f"{'Sortino':>{w}}{'MaxDD':>{w}}")
    for leg in ("overnight", "intraday", "buyhold"):
        for p in ("IS", "OOS"):
            m = bench[leg][p]
            print(f"  {leg:<14}{p:<5}{m['net_profit']:>{w},.2f}{m['total_return']:>{w-1}.1%} "
                  f"{m['sharpe']:>{w}.3f}{m['sortino']:>{w}.3f}{m['max_drawdown']:>{w-1}.1%} ")
    print("  (overnight ∘ intraday = buyhold; compares where return/risk live)\n")

    # --- B: Donchian, sweep on IS, lock best, report ---
    best = None
    for n in _N_GRID:
        for k in _K_GRID:
            m, _ = run_donchian(daily, n=n, k=k, slip_bps=args.slip_bps, capital=args.capital)
            is_net = m["IS"]["net_profit"]
            if best is None or is_net > best[0]:
                best = (is_net, n, k)
    _, bn, bk = best
    m, sym_net = run_donchian(daily, n=bn, k=bk, slip_bps=args.slip_bps, capital=args.capital)
    print(f"[Donchian sweep] best-by-IS-net config: N={bn}, k={bk} (IS net ${best[0]:,.2f})\n")
    _print_block(f"B Donchian (N={bn}, k={bk})", m["IS"], m["OOS"], _TRADE_KEYS,
                 _pct_symbols_positive(sym_net["IS"]), _pct_symbols_positive(sym_net["OOS"]))
    verdicts[f"B Donchian (N={bn}, k={bk})"] = (m["OOS"], _pct_symbols_positive(sym_net["OOS"]))

    # --- Verdict (balanced robustness, judged on OOS) ---
    print("VERDICT — balanced robustness, judged on OOS (recent held-out year):")
    print("  Deployable = OOS net>0 AND majority of symbols positive AND profit factor>=1.2\n")
    for name, (oos, pos) in verdicts.items():
        net = oos["net_profit"]
        pf = oos.get("profit_factor")
        p, total = (int(x) for x in pos.split("/"))
        ok = net > 0 and p > total / 2 and (pf is not None and pf >= 1.2)
        pf_s = "n/a" if pf is None else f"{pf:.2f}"
        print(f"  {name:<30} OOS net ${net:>9,.2f}  PF={pf_s:<5} sym+={pos:<6} "
              f"→ {'DEPLOYABLE' if ok else 'NO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
