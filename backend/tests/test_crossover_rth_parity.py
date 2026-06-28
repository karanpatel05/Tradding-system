"""Invariant guard: with the RTH gate ON, the single-symbol portfolio sim still
reduces to signals.replay (risk gates neutralized). This is the regression test
for centralizing the session gate in decide_index — if a future change gates one
path but not the other, this fails.

Run: python -m tests.test_crossover_rth_parity
"""

from __future__ import annotations

import glob
import json
from dataclasses import replace


def _key(tr):
    return (tr.side, tr.entry_index, tr.exit_index, round(tr.entry_price, 6),
            round(tr.exit_price, 6), tr.reason)


def _run() -> None:
    from app.engine.risk import RiskLimits
    from app.strategies.ema_crossover.params import EmaCrossoverParams
    from app.strategies.ema_crossover.signals import replay
    from app.backtest.portfolio import simulate_portfolio

    files = sorted(glob.glob("data_cache/ibkr__*__5m.json"))
    if not files:
        print("SKIP test_crossover_rth_parity: no cached 5m bars "
              "(cache purged - repopulate from IBKR to run this test)")
        return
    bars = json.load(open(files[0])).get("bars", [])
    sym = files[0].split("__")[1]

    # RTH ON, HTF off (replay has no HTF gating to compare against), risk neutralized.
    p = replace(EmaCrossoverParams.from_settings(), use_rth_filter=True, use_htf_filter=False)
    neutral = RiskLimits(risk_per_trade_pct=1.0, max_positions=10**9,
                         max_portfolio_risk_pct=10**9, max_daily_loss_pct=10**9,
                         max_trades_per_day=10**9)

    rep = replay(bars, p, symbol=sym)
    sim = simulate_portfolio({sym: bars}, p, neutral, equity=10**12, direction="both")

    assert [_key(x) for x in rep] == [_key(x.trade) for x in sim], (
        f"replay vs sim diverged with RTH ON: {len(rep)} vs {len(sim)} trades")
    assert sim, "expected trades (RTH-on should still trade during the session)"
    print(f"PASS: replay == simulate_portfolio with RTH ON ({len(sim)} trades)")

    print("\nALL CROSSOVER RTH-PARITY TESTS PASSED")


if __name__ == "__main__":
    _run()
