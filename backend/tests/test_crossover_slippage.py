"""Cost model (order-type-aware): the live bracket exits a TARGET via a LIMIT order
(fills at price → no slippage) but a STOP/crossover/EOD via a MARKET order (crosses
the spread). So _portfolio_trade_dict charges slippage on the entry always, and on
the exit only when the exit isn't a 'target'. Commission is $0 (Tastytrade equities).

Run: python -m tests.test_crossover_slippage
"""

from __future__ import annotations


def _trade(reason, entry, exit_, stop, side="long"):
    from app.strategies.ema_crossover.signals import Trade
    return Trade(symbol="AMD", side=side, entry_index=1, exit_index=2,
                 entry_time=0, exit_time=300_000, entry_price=entry, exit_price=exit_,
                 stop=stop, target=exit_, reason=reason)


def _run() -> None:
    from app.backtest.portfolio import PortfolioTrade
    from app.strategies.ema_crossover.strategy import _portfolio_trade_dict

    # TARGET exit (limit fill): only the ENTRY leg is charged.
    tgt = PortfolioTrade(_trade("target", 100.0, 104.0, 98.0), shares=10)
    g = _portfolio_trade_dict(tgt, slippage_per_side_bps=0.0)
    assert (g["pnl"], g["return_pct"], g["r_multiple"]) == (40.0, 0.04, 2.0), g
    n = _portfolio_trade_dict(tgt, slippage_per_side_bps=5.0)
    # cost/share = 0.0005*100 = 0.05 → pnl 40 - 0.5 = 39.5; return drag 1*slip
    assert n["pnl"] == 39.5, n["pnl"]
    assert n["return_pct"] == 0.0395, n["return_pct"]
    assert n["r_multiple"] == 1.975, n["r_multiple"]
    print("PASS: target (limit) exit charged entry-leg slippage only")

    # STOP exit (market fill): BOTH legs charged.
    stp = PortfolioTrade(_trade("stop", 100.0, 98.0, 98.0), shares=10)
    s = _portfolio_trade_dict(stp, slippage_per_side_bps=5.0)
    # cost/share = 0.0005*(100+98) = 0.099 → pnl -20 - 0.99 = -20.99; return drag 2*slip
    assert s["pnl"] == -20.99, s["pnl"]
    assert s["return_pct"] == -0.021, s["return_pct"]
    print("PASS: stop (market) exit charged both legs")

    # crossover / eod exits are market closes → both legs too.
    xo = PortfolioTrade(_trade("crossover", 100.0, 101.0, 98.0), shares=10)
    assert _portfolio_trade_dict(xo, slippage_per_side_bps=5.0)["pnl"] == round(10.0 - 0.0005 * 201 * 10, 2)
    print("PASS: crossover/eod exits charged both legs")

    # default 0 bp → gross (back-compat).
    assert _portfolio_trade_dict(tgt)["pnl"] == 40.0
    print("PASS: default slippage is 0 (back-compat)")

    print("\nALL CROSSOVER SLIPPAGE TESTS PASSED")


if __name__ == "__main__":
    _run()
