"""optimize_grid threads slippage into its metrics, so 'max net_profit' is
net of cost. The same grid scored at 5 bp must net less than at 0 bp.

Run: python -m tests.test_optimizer_slippage
"""

from __future__ import annotations

import glob
import json
from dataclasses import replace


def _run() -> None:
    from app.backtest.optimizer import optimize_grid
    from app.engine.risk import RiskLimits
    from app.strategies.ema_crossover.params import EmaCrossoverParams

    files = sorted(glob.glob("data_cache/ibkr__*__5m.json"))
    if not files:
        print("SKIP test_optimizer_slippage: no cached 5m bars "
              "(cache purged - repopulate from IBKR to run this test)")
        return
    bars_by_symbol = {f.split("__")[1]: json.load(open(f)).get("bars", []) for f in files[:4]}

    base = replace(EmaCrossoverParams.from_settings(), use_htf_filter=False, use_rth_filter=True)
    limits = RiskLimits.from_settings()
    grid = {"risk_reward": [2.0, 3.0], "stop_atr_mult": [1.5]}

    common = dict(grid=grid, directions=["long"], objective="net_profit")
    gross = optimize_grid(bars_by_symbol, None, base, limits, 2000.0,
                          slippage_per_side_bps=0.0, **common)
    netted = optimize_grid(bars_by_symbol, None, base, limits, 2000.0,
                           slippage_per_side_bps=5.0, **common)

    g_top = gross[0]["metrics"]["net_profit"]
    n_top = netted[0]["metrics"]["net_profit"]
    assert n_top < g_top, f"expected 5bp net ({n_top}) < gross ({g_top})"
    print(f"PASS: optimizer is cost-aware — gross top {g_top:.2f} > 5bp top {n_top:.2f}")

    print("\nALL OPTIMIZER-SLIPPAGE TESTS PASSED")


if __name__ == "__main__":
    _run()
