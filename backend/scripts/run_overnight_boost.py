"""Can we beat A1 overnight drift toward >=30% annually *robustly*? Standalone, report only.

Objective (per review): leverage makes "hit 30%" trivial, so the real metric is **robust
OOS risk-adjusted return (Sharpe)** — then lever the best to the target and compare
drawdown. Reference = levered A1. A new idea earns its place only if it reaches 30% at
lower drawdown / higher Sharpe than levered A1.

Discipline: each idea is ONE pre-committed config, all choices made on IS (older year);
OOS (recent year) is revealed once. No leverage sweep on OOS, no best-of-N.

Candidates:
  A1     overnight drift, all 10, always-on            (reference)
  TOM    overnight, all 10, turn-of-month nights only  (last 1 + first 3 trading days)
  TopK   overnight, top-5 symbols by IS overnight Sharpe (selected on IS only)
  L/S    long overnight + short intraday, all 10        (market-neutral; one honest look)

Run:  python -m scripts.run_overnight_boost
"""

from __future__ import annotations

import math
from collections import defaultdict

from app.config import get_settings
from app.services import history_cache
from app.strategies.daily_anomaly.rth_daily import to_rth_daily
from scripts.run_daily_strategies import _period, _summary_returns

_SOURCE = "massive"
_TARGET_CAGR = 0.30
_MARGIN_RATE = 0.07   # annual financing on the borrowed (L-1) portion


def _load_daily(symbol: str) -> list[dict]:
    entry = history_cache.load(symbol, "5m", source=_SOURCE)
    return to_rth_daily(entry.get("bars", [])) if entry else []


def _legs(days: list[dict], slip: float):
    """Per-symbol date->return for overnight (long) and intraday (short), net of slippage."""
    ov, sh = {}, {}
    for i in range(1, len(days)):
        d, p = days[i], days[i - 1]
        if p["close"] <= 0 or d["open"] <= 0 or d["close"] <= 0:
            continue
        ov[d["date"]] = (d["open"] * (1 - slip)) / (p["close"] * (1 + slip)) - 1
        # short at open, cover at close
        sh[d["date"]] = (d["open"] * (1 - slip) - d["close"] * (1 + slip)) / d["open"]
    return ov, sh


def _tom_dates(all_dates: list[str], last: int = 1, first: int = 3) -> set[str]:
    """Turn-of-month trading days: last `last` of each month + first `first` of next."""
    by_month: dict[str, list[str]] = defaultdict(list)
    for d in sorted(set(all_dates)):
        by_month[d[:7]].append(d)
    keep: set[str] = set()
    for month in by_month.values():
        keep.update(month[:first])
        keep.update(month[-last:])
    return keep


def _sharpe(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return (m / sd) * math.sqrt(252) if sd > 0 else 0.0


def _lever_to_target(is_rets: list[float], target: float) -> float:
    """Smallest L in [1,3] whose IS CAGR (after financing) reaches `target`; chosen on IS."""
    def cagr(rets, L):
        eq, n = 1.0, len(rets)
        if n == 0:
            return 0.0
        for r in rets:
            eq *= (1 + L * r - (L - 1) * _MARGIN_RATE / 252)
        return eq ** (252 / n) - 1 if eq > 0 else -1.0
    if cagr(is_rets, 1.0) >= target:
        return 1.0
    lo, hi = 1.0, 3.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if cagr(is_rets, mid) < target:
            lo = mid
        else:
            hi = mid
    return round(hi, 2)


def _apply_lev(rets: list[float], L: float) -> list[float]:
    return [L * r - (L - 1) * _MARGIN_RATE / 252 for r in rets]


def main() -> int:
    symbols = get_settings().watchlist_symbols
    slip = 2e-4
    daily = {s: _load_daily(s) for s in symbols}
    daily = {s: d for s, d in daily.items() if d}

    # Per-symbol legs + all dates.
    legs = {s: _legs(d, slip) for s, d in daily.items()}
    all_dates = sorted({dt for ov, _ in legs.values() for dt in ov})
    tom = _tom_dates(all_dates)

    # Per-symbol IS overnight Sharpe → pick top-5 on IS only.
    is_sharpe = {}
    for s, (ov, _sh) in legs.items():
        is_sharpe[s] = _sharpe([r for dt, r in ov.items() if _period(dt) == "IS"])
    topk = {s for s, _ in sorted(is_sharpe.items(), key=lambda kv: kv[1], reverse=True)[:5]}

    def portfolio(selector, leg: str):
        by_date: dict[str, list[float]] = defaultdict(list)
        for s, (ov, sh) in legs.items():
            if s not in selector:
                continue
            for dt in ov:
                by_date[dt].append(ov[dt] + sh[dt] if leg == "ls" else ov[dt])
        return [(dt, sum(v) / len(v)) for dt, v in sorted(by_date.items()) if v]

    allsyms = set(legs)

    candidates = {
        "A1 overnight (all, always)": portfolio(allsyms, "ov"),
        "TOM overnight (all, turn-of-month)": [(dt, r) for dt, r in portfolio(allsyms, "ov") if dt in tom],
        "TopK overnight (IS top-5 Sharpe)": portfolio(topk, "ov"),
        "L/S long-overnight short-intraday": portfolio(allsyms, "ls"),
    }

    print(f"Overnight boost search | symbols={len(daily)} | target CAGR {_TARGET_CAGR:.0%} "
          f"| slip 2bp/side | margin {_MARGIN_RATE:.0%}")
    print(f"  TopK (by IS overnight Sharpe): {', '.join(sorted(topk))}")
    print("  Discipline: config fixed on IS; OOS revealed once. Leverage chosen on IS.\n")

    w = 13
    hdr = ("Candidate", "L*", "IS CAGR", "IS Shrp", "IS DD", "OOS CAGR", "OOS Shrp", "OOS Sort", "OOS DD")
    print("  " + f"{hdr[0]:<34}" + "".join(f"{h:>{w}}" for h in hdr[1:]))
    print("  " + "-" * (34 + w * 8))

    rows = []
    for name, series in candidates.items():
        is_r = [r for dt, r in series if _period(dt) == "IS"]
        oos_r = [r for dt, r in series if _period(dt) == "OOS"]
        L = _lever_to_target(is_r, _TARGET_CAGR)
        is_m = _summary_returns(_apply_lev(is_r, L), capital=2000)
        oos_m = _summary_returns(_apply_lev(oos_r, L), capital=2000)
        rows.append((name, L, oos_m))
        print("  " + f"{name:<34}"
              f"{L:>{w}.2f}{is_m['cagr']:>{w-1}.1%} {is_m['sharpe']:>{w}.2f}{is_m['max_drawdown']:>{w-1}.1%} "
              f"{oos_m['cagr']:>{w-1}.1%} {oos_m['sharpe']:>{w}.2f}{oos_m['sortino']:>{w}.2f}"
              f"{oos_m['max_drawdown']:>{w-1}.1%} ")

    # Reference = A1's OOS Sharpe. Winner = highest robust OOS Sharpe.
    ref = rows[0][2]["sharpe"]
    print(f"\n  Reference = levered A1, OOS Sharpe {ref:.2f}, OOS CAGR {rows[0][2]['cagr']:.1%}, "
          f"OOS maxDD {rows[0][2]['max_drawdown']:.1%}")
    print("  A new idea wins only if OOS Sharpe > A1 AND it reaches ~30% (IS-set L) at <= A1 drawdown.\n")
    best = max(rows, key=lambda r: r[2]["sharpe"])
    print("VERDICT (judged on robust OOS Sharpe, not raw return):")
    for name, L, oos in rows:
        verdict = "beats A1" if oos["sharpe"] > ref + 1e-9 else "no better than A1"
        hit30 = "yes" if oos["cagr"] >= _TARGET_CAGR - 1e-9 else "below 30%"
        print(f"  {name:<34} L*={L:<4} OOS Sharpe {oos['sharpe']:>5.2f}  OOS CAGR {oos['cagr']:>6.1%}  "
              f"[{hit30}; {verdict}]")
    print(f"\n  Best robust OOS Sharpe: {best[0]} (Sharpe {best[2]['sharpe']:.2f}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
