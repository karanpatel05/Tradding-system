"""Portfolio backtest tests (pure, deterministic).

Run: python -m tests.test_portfolio_backtest

Discriminating tests that prove PARITY, not just "it runs":
- portfolio-sim == signals.replay when all risk gates are neutralized (the guard
  for the shared decide_index refactor + sizing layer),
- sizing parity: the sim sizes shares via the same RiskEngine the live worker uses,
- ET day rollover: the per-day trade cap resets on a new ET day (proves bar
  timestamps are threaded as ``now`` into the engine),
- shared buying-power pool across symbols: an open position blocks a concurrent
  one and releases it on exit,
- direction modes (long / short / both),
- metrics shape incl. avg_hold.
"""

from __future__ import annotations

from dataclasses import replace

from app.backtest.engine import _summarize_portfolio
from app.engine.risk import RiskEngine, RiskLimits
from app.strategies.ema_crossover.params import EmaCrossoverParams
from app.strategies.ema_crossover.signals import Position, replay, resolve_intrabar_exit
from app.strategies.ema_crossover.strategy import _portfolio_trade_dict
from app.backtest.portfolio import simulate_portfolio, _group_1m_by_5m
from tests.test_crossover_signals import long_setup, short_setup

_STEP = 300_000  # 5 min in ms
_NEUTRAL = RiskLimits(risk_per_trade_pct=1.0, max_positions=10**9,
                      max_portfolio_risk_pct=10**9, max_daily_loss_pct=10**9,
                      max_trades_per_day=10**9)


def _params() -> EmaCrossoverParams:
    # Sizing/pool/cap/parity mechanics on synthetic (out-of-RTH) timestamps — disable
    # the RTH gate here; RTH parity on real bars is covered in test_crossover_rth_parity.
    return replace(EmaCrossoverParams.from_settings(), use_rth_filter=False)


def _retime(bars, offset_ms):
    return [{**b, "time": b["time"] + offset_ms} for b in bars]


def _key(tr):
    return (tr.side, tr.entry_index, tr.exit_index, round(tr.entry_price, 6),
            round(tr.exit_price, 6), tr.reason)


def _run() -> None:
    p = _params()

    # --- 1) portfolio-sim == replay with gates neutralized -------------------
    bars = long_setup()
    rep = replay(bars, p, symbol="X")
    sim = simulate_portfolio({"X": bars}, p, _NEUTRAL, equity=10**12,
                             direction="both", watchlist_order=["X"])
    assert [_key(x) for x in rep] == [_key(x.trade) for x in sim], (
        [_key(x) for x in rep], [_key(x.trade) for x in sim])
    assert sim, "expected a trade"
    print(f"PASS: portfolio sim == replay ({len(sim)} trade(s), gates neutralized)")

    # --- 2) sizing parity: shares == RiskEngine.check_and_size --------------
    equity = 5000.0
    limits = RiskLimits.from_settings()
    one = simulate_portfolio({"X": bars}, p, limits, equity=equity,
                             direction="long", watchlist_order=["X"])
    assert len(one) == 1, one
    tr = one[0]
    expect = RiskEngine(equity, limits).check_and_size(
        "X", tr.trade.entry_price, tr.trade.stop)
    assert tr.shares == expect.quantity > 0, (tr.shares, expect)
    # $-P&L uses the live formula on the engine-sized shares.
    assert abs(tr.pnl - (tr.trade.exit_price - tr.trade.entry_price) * tr.shares) < 1e-6
    print(f"PASS: sizing parity — sim sized {tr.shares} sh (= RiskEngine), pnl ${tr.pnl:.2f}")

    # --- 3) ET day rollover resets the per-day trade cap --------------------
    DAYA = 1_709_300_000_000
    DAYB = DAYA + 86_400_000

    def _block(start, dn=25, rn=20, ds=0.4, rs=0.9):
        cl = [start - k * ds for k in range(dn)]
        base = cl[-1]
        return cl + [base + (k + 1) * rs for k in range(rn)]

    closes = _block(110.0) + _block(110.0 - 24 * 0.4 + 20 * 0.9)  # two long setups
    vols = ([1000] * 25 + [5000] * 20) * 2

    def _bars(times):
        return [{"time": times[i], "open": c, "high": c + 0.3, "low": c - 0.3,
                 "close": c, "volume": vols[i]} for i, c in enumerate(closes)]

    multiday = _bars([DAYA + i * _STEP if i < 45 else DAYB + (i - 45) * _STEP
                      for i in range(len(closes))])
    singleday = _bars([DAYA + i * _STEP for i in range(len(closes))])
    cap1 = RiskLimits(1.0, 10**9, 10**9, 10**9, 1)  # max 1 trade/day
    n_multi = len(simulate_portfolio({"X": multiday}, p, cap1, 10**12,
                                     direction="long", watchlist_order=["X"]))
    n_single = len(simulate_portfolio({"X": singleday}, p, cap1, 10**12,
                                      direction="long", watchlist_order=["X"]))
    assert n_multi == 2, f"two ET days, cap 1/day -> 2 trades, got {n_multi}"
    assert n_single == 1, f"one ET day, cap 1/day -> 1 trade, got {n_single}"
    print(f"PASS: ET rollover — multiday={n_multi} trades, singleday(cap)={n_single} (now threaded)")

    # --- 4) shared buying-power pool across symbols -------------------------
    aaa = long_setup()
    pool_lim = RiskLimits(1.0, 10**9, 10**9, 10**9, 10**9)  # cash-bound
    E = 2000.0
    overlap = simulate_portfolio({"AAA": aaa, "BBB": _retime(aaa, 0)}, p, pool_lim, E,
                                 direction="long", watchlist_order=["AAA", "BBB"])
    stagger = simulate_portfolio({"AAA": aaa, "BBB": _retime(aaa, len(aaa) * _STEP)},
                                 p, pool_lim, E, direction="long",
                                 watchlist_order=["AAA", "BBB"])
    assert {x.trade.symbol for x in overlap} == {"AAA"}, overlap  # BBB blocked while AAA holds pool
    assert {x.trade.symbol for x in stagger} == {"AAA", "BBB"}, stagger  # pool freed after AAA exits
    assert all(x.capital_used <= E + 1e-6 for x in overlap)  # pool never double-spent
    print("PASS: shared pool — concurrent entry blocked, released on exit")

    # --- 5) direction modes -------------------------------------------------
    assert len(simulate_portfolio({"X": long_setup()}, p, _NEUTRAL, 10**12,
                                  direction="long", watchlist_order=["X"])) == 1
    assert simulate_portfolio({"X": long_setup()}, p, _NEUTRAL, 10**12,
                              direction="short", watchlist_order=["X"]) == []
    assert len(simulate_portfolio({"X": short_setup()}, p, _NEUTRAL, 10**12,
                                  direction="short", watchlist_order=["X"])) == 1
    assert simulate_portfolio({"X": short_setup()}, p, _NEUTRAL, 10**12,
                              direction="long", watchlist_order=["X"]) == []
    print("PASS: direction modes — long/short/both filter sides")

    # --- 6) summarizer shape incl. avg_hold --------------------------------
    rows = [_portfolio_trade_dict(x) for x in one]
    out = _summarize_portfolio(rows, equity, stocks_scanned=1)
    for k in ("net_profit", "sharpe", "expectancy", "avg_r_multiple", "avg_hold",
              "max_drawdown", "profit_factor", "win_rate"):
        assert k in out["metrics"], (k, out["metrics"].keys())
    for k in ("equity_curve", "daily_pnl", "drawdown", "monthly", "durations"):
        assert k in out["charts"], (k, out["charts"].keys())
    assert out["results"]["trades_taken"] == len(rows)
    assert out["metrics"]["avg_hold"] >= 0.0
    # net_profit equals the summed risk-sized P&L (sizing parity reaches the report).
    assert abs(out["metrics"]["net_profit"] - round(tr.pnl, 2)) < 0.01, out["metrics"]["net_profit"]
    print(f"PASS: summarizer shape (avg_hold={out['metrics']['avg_hold']}, net=${out['metrics']['net_profit']})")

    _test_intrabar_resolver()
    _test_1m_flips_straddle()
    print("\nALL PORTFOLIO BACKTEST TESTS PASSED")


def _b(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1000}


def _test_intrabar_resolver() -> None:
    """resolve_intrabar_exit: first touch wins, stop-first within a bar, both sides."""
    long_pos = Position("long", entry_price=100.0, stop=98.0, target=104.0,
                        entry_index=0, entry_time=0)
    # target touched on bar 2, nothing before → exit target on bar 2.
    a, b = resolve_intrabar_exit(long_pos, [_b(0, 100, 101, 99.5, 100.5),
                                            _b(1, 100.5, 103, 100, 102),
                                            _b(2, 102, 104.2, 101.5, 104)])
    assert a and a.reason == "target" and a.price == 104.0 and b["time"] == 2, (a, b)
    # stop touched first (bar 0) even though a later bar hits target.
    a, _ = resolve_intrabar_exit(long_pos, [_b(0, 100, 100.2, 97.5, 98), _b(1, 98, 105, 98, 104)])
    assert a and a.reason == "stop" and a.price == 98.0, a
    # within ONE bar that straddles both, stop wins (conservative tie-break).
    a, _ = resolve_intrabar_exit(long_pos, [_b(0, 100, 104.5, 97.0, 101)])
    assert a and a.reason == "stop", a
    # short mirror + no-touch → None.
    short_pos = Position("short", entry_price=100.0, stop=102.0, target=96.0,
                         entry_index=0, entry_time=0)
    a, _ = resolve_intrabar_exit(short_pos, [_b(0, 100, 100.5, 95.5, 96)])
    assert a and a.reason == "target" and a.price == 96.0, a
    assert resolve_intrabar_exit(long_pos, [_b(0, 100, 101, 99, 100.5)]) == (None, None)
    # grouping: 5m window holds its [t, t+5m) minute bars, alignment-agnostic.
    g = _group_1m_by_5m([{"time": 0}, {"time": 300_000}],
                        [{"time": 0}, {"time": 60_000}, {"time": 299_999}, {"time": 300_000}])
    assert len(g[0]) == 3 and len(g[300_000]) == 1, g
    print("PASS: resolve_intrabar_exit (first-touch, stop-first tie, both sides) + 5m grouping")


def _test_1m_flips_straddle() -> None:
    """The discriminating test: a 5m exit bar that straddles BOTH stop and target
    resolves to 'stop' on 5m-only (pessimistic), but to 'target' when 1m shows the
    target was hit first — proving 1m intrabar resolution changes the fill."""
    p = _params()
    bars5 = [dict(b) for b in long_setup()]
    base = simulate_portfolio({"X": bars5}, p, _NEUTRAL, 10**12,
                              direction="long", watchlist_order=["X"])
    assert base and base[0].trade.reason == "target", base
    pt = base[0].trade
    E = pt.exit_index
    # Make the 5m exit bar straddle: keep high ≥ target, push low ≤ stop.
    bars5[E] = {**bars5[E], "low": pt.stop - 0.5, "high": max(bars5[E]["high"], pt.target + 0.5)}

    only5m = simulate_portfolio({"X": bars5}, p, _NEUTRAL, 10**12,
                                direction="long", watchlist_order=["X"])
    assert only5m[0].trade.reason == "stop", only5m[0].trade.reason  # pessimistic 5m

    # 1m for window E: target is touched on the FIRST minute, stop only later → target.
    t0 = bars5[E]["time"]
    w = [_b(t0, pt.entry_price, pt.target + 0.1, pt.entry_price - 0.2, pt.target),      # target first
         _b(t0 + 60_000, pt.target, pt.target, pt.stop - 0.5, pt.stop)]                 # stop later
    with_1m = simulate_portfolio({"X": bars5}, p, _NEUTRAL, 10**12, direction="long",
                                 watchlist_order=["X"], bars_1m_by_symbol={"X": w})
    assert with_1m[0].trade.reason == "target", with_1m[0].trade.reason
    assert with_1m[0].trade.exit_price == pt.target
    assert with_1m[0].trade.exit_time == t0  # filled at the 1m timestamp, mid-5m-window
    print("PASS: 1m intrabar resolution flips a straddle 5m='stop' → 1m='target'")


if __name__ == "__main__":
    _run()
