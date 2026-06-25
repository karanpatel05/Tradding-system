"""Run the Triple Calendar backtest on Polygon option data. Standalone, report only.

    python -m scripts.run_triple_calendar               # QQQ, ~last 2y
    python -m scripts.run_triple_calendar SPY 2024-07-01 2026-06-01
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from app.strategies.triple_calendar.backtest import compute_metrics, run_backtest
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.pricing import PolygonPriceSource


def main() -> int:
    underlying = sys.argv[1].upper() if len(sys.argv) > 1 else "QQQ"
    start = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() - timedelta(days=730)
    end = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.today()
    params = TripleCalendarParams(underlying=underlying)
    source = PolygonPriceSource(underlying, start, end)

    print(f"Triple Calendar backtest | {underlying} | {start} -> {end} "
          f"| sell {params.sell_dte}DTE / buy {params.buy_dte}DTE | target {params.profit_target:.0%}")
    trades = run_backtest(source, params, start, end)
    m = compute_metrics(trades)
    print(f"\nTrades {m['total_trades']} | win {m['win_rate']:.0%} | avg P&L "
          f"{m['avg_pnl_pct']:.1%} | avg hold {m['avg_hold_days']}d | maxDD {m['max_drawdown']:.1%}")
    if m["iv_regime"]:
        print("IV regime:")
        for bucket, r in sorted(m["iv_regime"].items()):
            print(f"  {bucket:<14} trades {r['trades']:>3}  win {r['win_rate']:.0%}  avg {r['avg_pnl_pct']:+.1%}")
    if not trades:
        print("\n(No completed trades — option history may be too thin for the chosen "
              "expiries/window on the current Polygon plan.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
