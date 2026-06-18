"""Volume-confirmed Donchian breakout (1h, 2×ATR trailing stop) — full OOS treatment.
Standalone, report only. 10 stocks (5m→1h) + 2 indices SPY/QQQ (15m→1h).

Per-trade P&L is in R units (1R = the initial 2×ATR risk); $ assumes 1% capital risk per
trade (so net$ = ΣR × $20 on $2,000). Split IS<2025-06-05 / OOS>=2025-06-05.

Run:  python -m scripts.run_vol_donchian
"""

from __future__ import annotations

from collections import defaultdict

from app.config import get_settings
from app.services import history_cache
from app.strategies.breakout.volume_donchian import aggregate_hourly, simulate
from scripts.run_daily_strategies import _period, _profit_factor

_RISK_DOLLARS = 20.0  # 1% of $2,000 per trade


def _hourly(symbol: str, interval: str) -> list[dict]:
    entry = history_cache.load(symbol, interval, source="massive")
    return aggregate_hourly(entry.get("bars", [])) if entry else []


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
    return {
        "n": n, "net": sum(rs) * _RISK_DOLLARS,
        "win_rate": len(wins) / n,
        "expectancy_r": sum(rs) / n,
        "avg_win_r": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": _profit_factor(rs),
        "long_pct": sum(1 for t in trades if t["side"] == "long") / n,
        "stop_pct": reasons["trail_stop"] / n,
        "rev_pct": reasons["reverse"] / n,
    }


def main() -> int:
    stocks = get_settings().watchlist_symbols
    universe = [(s, "5m") for s in stocks] + [("SPY", "15m"), ("QQQ", "15m")]

    by_period = {"IS": [], "OOS": []}
    sym_net = {"IS": defaultdict(float), "OOS": defaultdict(float)}
    missing, spans = [], []
    for sym, interval in universe:
        bars = _hourly(sym, interval)
        if len(bars) < 60:
            missing.append(sym)
            continue
        spans.append((bars[0]["date"], bars[-1]["date"]))
        for t in simulate(bars, channel=20, vol_mult=1.5, atr_mult=2.0, slip_bps=2.0):
            p = _period(t["entry_date"])
            by_period[p].append(t)
            sym_net[p][sym] += t["pnl_r"] * _RISK_DOLLARS

    n_sym = len(universe) - len(missing)
    print(f"Volume Donchian breakout | 1h candles | {n_sym} symbols (10 stocks + SPY/QQQ)")
    print("  20-period high/low + 1.5× vol confirm | long+short | 2×ATR trailing stop | 2bp/side")
    if spans:
        print(f"  range {min(s[0] for s in spans)}→{max(s[1] for s in spans)}")
    if missing:
        print(f"  (skipped, insufficient data: {', '.join(missing)})")
    print(f"  IS<2025-06-05 / OOS>=2025-06-05\n")

    w = 16
    print(f"  {'Metric':<18}{'IS (older)':>{w}}{'OOS (recent)':>{w}}")
    print(f"  {'-'*18}{'-'*w}{'-'*w}")
    m = {p: _stats(by_period[p]) for p in ("IS", "OOS")}
    rows = [("Trades", "n", "{:,}"), ("Net $ (1% risk)", "net", "${:,.2f}"),
            ("Win rate", "win_rate", "{:.1%}"), ("Expectancy (R)", "expectancy_r", "{:+.3f}"),
            ("Avg win (R)", "avg_win_r", "{:+.2f}"), ("Avg loss (R)", "avg_loss_r", "{:+.2f}"),
            ("Profit factor", "profit_factor", "{:.3f}"), ("% long", "long_pct", "{:.0%}"),
            ("% trail-stop", "stop_pct", "{:.0%}"), ("% reversed", "rev_pct", "{:.0%}")]
    for lbl, key, fmt in rows:
        a, b = m["IS"].get(key), m["OOS"].get(key)
        fa = "n/a" if a is None else fmt.format(a)
        fb = "n/a" if b is None else fmt.format(b)
        print(f"  {lbl:<18}{fa:>{w}}{fb:>{w}}")
    pos_is = sum(1 for v in sym_net["IS"].values() if v > 0)
    pos_oos = sum(1 for v in sym_net["OOS"].values() if v > 0)
    print(f"  {'Symbols +':<18}{f'{pos_is}/{n_sym}':>{w}}{f'{pos_oos}/{n_sym}':>{w}}\n")

    oos = m["OOS"]
    pf = oos.get("profit_factor")
    ok = oos.get("net", 0) > 0 and pos_oos > n_sym / 2 and pf is not None and pf >= 1.2
    print("VERDICT — balanced robustness, judged on OOS:")
    print(f"  OOS net ${oos.get('net', 0):,.2f} | win {oos.get('win_rate', 0):.1%} | "
          f"expectancy {oos.get('expectancy_r', 0):+.3f}R | "
          f"PF {('n/a' if pf is None else f'{pf:.2f}')} | sym+ {pos_oos}/{n_sym}")
    print(f"  → {'DEPLOYABLE' if ok else 'NO'} (need OOS net>0, PF>=1.2, majority symbols +)")

    # Stocks vs indices split (the strategy is classically a stock/trend tool).
    print("\n  Breakdown OOS net $ by symbol:")
    for sym in sorted(sym_net["OOS"], key=lambda s: sym_net["OOS"][s]):
        print(f"    {sym:<6} ${sym_net['OOS'][sym]:>8,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
