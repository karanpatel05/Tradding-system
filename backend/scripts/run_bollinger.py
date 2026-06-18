"""Bollinger intraday mean reversion on SPY/QQQ (15m) — full OOS treatment. Report only.

Spec under test: 15m candles, 20-period SMA + (population) std. Long when close > num_std
below the mean, short when num_std above; exit when close returns to the mean. Thresholds:
SPY 1.5σ, QQQ 1.8σ. Two exit regimes reported: literal (hold to mean, even overnight) and
intraday (force-flat at the close each day). Per-trade stats + compounded return, IS vs OOS.

Run:  python -m scripts.run_bollinger
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app.services import history_cache
from app.strategies.mean_reversion.bollinger import simulate_bollinger
from scripts.run_daily_strategies import _period, _profit_factor

_ET = ZoneInfo("America/New_York")
_OPEN, _CLOSE = time(9, 30), time(16, 0)
_SYMS = {"SPY": 1.5, "QQQ": 1.8}


def _rth_bars(symbol: str) -> list[dict]:
    """Load 15m massive bars, keep RTH only, attach ET 'date' for eod logic."""
    entry = history_cache.load(symbol, "15m", source="massive")
    if not entry:
        return []
    out = []
    for b in entry.get("bars", []):
        dt = datetime.fromtimestamp(b["time"] / 1000, tz=timezone.utc).astimezone(_ET)
        if _OPEN <= dt.time() < _CLOSE:
            out.append({**b, "date": dt.date().isoformat()})
    out.sort(key=lambda x: x["time"])
    return out


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    longs = sum(1 for t in trades if t["side"] == "long")
    return {
        "n": n, "total_return": eq - 1,
        "win_rate": len(wins) / n,
        "expectancy": sum(rets) / n,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": _profit_factor(rets),
        "avg_hold": sum(t["bars_held"] for t in trades) / n,
        "long_pct": longs / n,
    }


def _print(symbol: str, sigma: float, label: str, trades: list[dict]) -> dict:
    per = {"IS": [t for t in trades if _period(t["entry_date"]) == "IS"],
           "OOS": [t for t in trades if _period(t["entry_date"]) == "OOS"]}
    m = {p: _stats(per[p]) for p in ("IS", "OOS")}
    print(f"--- {symbol} @ {sigma}σ — {label} ---")
    w = 16
    print(f"  {'Metric':<16}{'IS (older)':>{w}}{'OOS (recent)':>{w}}")
    print(f"  {'-'*16}{'-'*w}{'-'*w}")
    rows = [("Trades", "n", "{:,}"), ("Total return", "total_return", "{:.2%}"),
            ("Win rate", "win_rate", "{:.1%}"), ("Profit factor", "profit_factor", "{:.3f}"),
            ("Expectancy", "expectancy", "{:.4%}"), ("Avg win", "avg_win", "{:.3%}"),
            ("Avg loss", "avg_loss", "{:.3%}"), ("Avg hold (bars)", "avg_hold", "{:.1f}"),
            ("% long", "long_pct", "{:.0%}")]
    for lbl, key, fmt in rows:
        a, b = m["IS"].get(key), m["OOS"].get(key)
        fa = "n/a" if a is None else fmt.format(a)
        fb = "n/a" if b is None else fmt.format(b)
        print(f"  {lbl:<16}{fa:>{w}}{fb:>{w}}")
    print()
    return m["OOS"]


def main() -> int:
    print("Bollinger intraday mean reversion | 15m RTH | 20-period | pop. std | 2bp/side")
    print("  SPY 1.5σ, QQQ 1.8σ | exit at mean | IS<2025-06-05 / OOS>=2025-06-05\n")
    missing = [s for s in _SYMS if not _rth_bars(s)]
    if missing:
        print(f"ERROR: no cached 15m data for {', '.join(missing)} — run "
              f"`python -m scripts.predownload_1m --interval 15m {' '.join(missing)}` first.")
        return 1

    verdict = {}
    for sym, sigma in _SYMS.items():
        bars = _rth_bars(sym)
        d0, d1 = bars[0]["date"], bars[-1]["date"]
        print(f"=== {sym} === ({len(bars):,} RTH 15m bars, {d0}→{d1})")
        lit = simulate_bollinger(bars, window=20, num_std=sigma, slip_bps=2.0, eod_flat=False)
        intr = simulate_bollinger(bars, window=20, num_std=sigma, slip_bps=2.0, eod_flat=True)
        verdict[(sym, "literal")] = _print(sym, sigma, "literal (hold to mean)", lit)
        verdict[(sym, "intraday")] = _print(sym, sigma, "intraday (EOD flat)", intr)

    print("VERDICT — judged on OOS (recent year):")
    for (sym, var), m in verdict.items():
        if m.get("n", 0) == 0:
            print(f"  {sym:<4} {var:<9} no trades")
            continue
        pf = m["profit_factor"]
        ok = m["total_return"] > 0 and pf is not None and pf >= 1.2
        print(f"  {sym:<4} {var:<9} OOS ret {m['total_return']:>7.1%}  win {m['win_rate']:>5.1%}  "
              f"PF {('n/a' if pf is None else f'{pf:.2f}'):<5}  trades {m['n']:>4}  "
              f"→ {'PROFITABLE' if ok else 'NO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
