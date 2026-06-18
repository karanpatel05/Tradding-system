"""Optimizer tests (deterministic).

Run: DATABASE_URL=sqlite+aiosqlite:///./_test_opt.db python -m tests.test_optimizer

Discriminating checks:
- ranking order matches the chosen objective (and a different objective re-ranks),
- grid validation (whitelist / non-empty / numeric / combo cap),
- FETCH-ONCE: strategy.optimize fetches each symbol's bars exactly once across all
  grid combos (re-fetching per combo would be slow AND unfair — combos could see
  different bars). Verified with a fetch counter.
"""

from __future__ import annotations

import asyncio

from app.backtest.optimizer import (
    combo_count, optimize_grid, validate_grid, MAX_COMBOS,
)
from app.engine.risk import RiskLimits
from app.strategies.ema_crossover.params import EmaCrossoverParams
from tests.test_crossover_signals import long_setup, short_setup


def _params() -> EmaCrossoverParams:
    return EmaCrossoverParams.from_settings()


_NEUTRAL = RiskLimits(1.0, 10**9, 10**9, 10**9, 10**9)


def _test_validate_grid() -> None:
    assert validate_grid({"risk_reward": [1.5, 2.0]}) == {"risk_reward": [1.5, 2.0]}
    assert validate_grid({"fast": [9, 12]}) == {"fast": [9, 12]}  # int coercion
    for bad in ({"nonsense": [1]}, {"risk_reward": []}, {"risk_reward": ["x"]}):
        try:
            validate_grid(bad); assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass
    assert combo_count({"risk_reward": [1, 2, 3]}, ["long", "both"]) == 6
    print("PASS: grid validation (whitelist / non-empty / numeric) + combo_count")


def _test_combo_cap() -> None:
    big = {"risk_reward": list(range(40)), "stop_atr_mult": list(range(40))}  # 1600 combos
    try:
        optimize_grid({"X": long_setup()}, None, _params(), _NEUTRAL, 10**9,
                      grid=big, directions=["both"])
        assert False, "expected combo-cap ValueError"
    except ValueError as e:
        assert "too large" in str(e), e
    print(f"PASS: combo cap rejects > {MAX_COMBOS} combinations")


def _test_ranking() -> None:
    # Long fixture trades on the long side; short fixture on the short side. With
    # both in the universe, direction=long vs short produce different P&L, so the
    # objective ranking must order them and re-rank when the objective changes.
    bars = {"UP": long_setup(), "DN": short_setup()}
    p = _params()
    res = optimize_grid(bars, None, p, _NEUTRAL, 10**9,
                        grid={"risk_reward": [1.5, 3.0]}, directions=["long", "short", "both"],
                        objective="net_profit", watchlist_order=["UP", "DN"])
    assert res, "expected results"
    vals = [r["objective_value"] for r in res]
    assert vals == sorted(vals, reverse=True), vals  # ranked best-first
    assert res[0]["rank"] == 1 and res[-1]["rank"] == len(res)
    assert res[0]["objective_value"] >= res[1]["objective_value"]
    # Every combo carries its overrides + direction + a metrics dict.
    assert all(set(r) >= {"params", "direction", "metrics", "trade_count"} for r in res)
    # Re-ranking by a different objective can change the winner ordering.
    res2 = optimize_grid(bars, None, p, _NEUTRAL, 10**9,
                         grid={"risk_reward": [1.5, 3.0]}, directions=["long", "short", "both"],
                         objective="win_rate", watchlist_order=["UP", "DN"])
    v2 = [r["objective_value"] for r in res2]
    assert v2 == sorted(v2, reverse=True), v2
    print(f"PASS: ranking by objective ({len(res)} combos, net_profit + win_rate both sorted)")

    # A swept param must actually move the result (guard against it being ignored):
    # two combos differing only in risk_reward (same direction) → different objective.
    long_only = [r for r in optimize_grid(
        {"UP": long_setup()}, None, p, _NEUTRAL, 10**9,
        grid={"risk_reward": [1.5, 3.0]}, directions=["long"], objective="net_profit",
        watchlist_order=["UP"]) ]
    by_rr = {r["params"]["risk_reward"]: r["objective_value"] for r in long_only}
    assert by_rr[1.5] != by_rr[3.0], by_rr  # risk_reward threads through to the sim
    print(f"PASS: swept risk_reward moves net_profit ({by_rr})")


def _test_objective_degeneracy() -> None:
    """The objectives net_profit/win_rate sidestep two traps that the others hit:
    a no-loss combo (profit_factor=None) and a no-trade combo (metrics all 0)."""
    p = _params()
    # vol_multiplier 100× average never fires → that combo takes zero trades.
    grid = {"vol_multiplier": [1.5, 100.0]}

    # max_drawdown: the no-trade combo has dd 0.0; trading combos are ≤ 0. The
    # do-nothing combo must NOT win.
    dd = optimize_grid({"UP": long_setup(), "DN": short_setup()}, None, p, _NEUTRAL, 10**9,
                       grid=grid, directions=["both"], objective="max_drawdown",
                       watchlist_order=["UP", "DN"])
    assert dd[0]["trade_count"] > 0, [(_r["params"], _r["trade_count"]) for _r in dd]
    no_trade = [r for r in dd if r["trade_count"] == 0]
    assert no_trade and no_trade[0]["rank"] == len(dd), no_trade
    print("PASS: max_drawdown — do-nothing (no-trade) combo ranked last, not first")

    # profit_factor: long_setup is a single winning trade → no losses → pf None →
    # must rank as BEST, above the no-trade combo.
    pf = optimize_grid({"UP": long_setup()}, None, p, _NEUTRAL, 10**9,
                       grid=grid, directions=["long"], objective="profit_factor",
                       watchlist_order=["UP"])
    assert pf[0]["trade_count"] > 0 and pf[0]["metrics"]["profit_factor"] is None, pf[0]
    assert pf[-1]["trade_count"] == 0, pf[-1]
    print("PASS: profit_factor — flawless (no-loss) combo ranked first, not last")


def _test_fetch_once() -> None:
    """strategy.optimize must fetch each symbol's bars exactly once for the WHOLE
    sweep — not once per combo."""
    from app.strategies.ema_crossover.strategy import EmaCrossoverStrategy

    strat = EmaCrossoverStrategy()
    calls: dict[str, int] = {}

    async def fake_fetch(symbol):
        calls[symbol] = calls.get(symbol, 0) + 1
        return long_setup() if symbol == "UP" else short_setup()

    strat.fetch_intraday = fake_fetch  # monkeypatch the network fetch

    async def go():
        return await strat.optimize(
            ["UP", "DN"], grid={"risk_reward": [1.5, 2.0, 2.5], "stop_atr_mult": [1.0, 1.5]},
            directions=["long", "short", "both"], objective="net_profit", capital=10**9)

    out = asyncio.run(go())
    n_combos = out["combos"]
    assert n_combos == 3 * 2 * 3, n_combos  # 6 grid combos × 3 directions = 18
    assert len(out["results"]) == n_combos, (len(out["results"]), n_combos)
    # The key assertion: one fetch per symbol despite 18 sims.
    assert calls == {"UP": 1, "DN": 1}, f"expected one fetch per symbol, got {calls}"
    print(f"PASS: fetch-once — {sum(calls.values())} fetches drove {n_combos} sim combos")


def _run() -> None:
    _test_validate_grid()
    _test_combo_cap()
    _test_ranking()
    _test_objective_degeneracy()
    _test_fetch_once()
    print("\nALL OPTIMIZER TESTS PASSED")


if __name__ == "__main__":
    _run()
