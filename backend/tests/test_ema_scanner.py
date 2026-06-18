"""EMA scalper scanner tests (pure filters + ranking).

Run: python -m tests.test_ema_scanner
"""

from __future__ import annotations

import asyncio

from app.strategies.ema_scalper.params import EmaScalperParams
from app.strategies.ema_scalper.scanner import build_watchlist, score_symbol
from tests.ema_fixtures import quiet_daily_bars, trending_daily_bars


def _run() -> None:
    p = EmaScalperParams.from_settings()

    # Trending, volatile, liquid name passes and is scored long.
    good = score_symbol(trending_daily_bars("MOVR"), p)
    assert good is not None, "trending name should pass the scan"
    assert good.symbol == "MOVR"
    assert good.metrics["direction"] == "long", good.metrics
    assert good.metrics["atr_pct"] >= p.min_atr_pct, good.metrics
    assert good.metrics["rel_volume"] >= p.min_rel_volume, good.metrics
    print(f"PASS: trending name passes (score={good.score}, {good.reason})")

    # Quiet, illiquid name fails the volatility / liquidity floors.
    bad = score_symbol(quiet_daily_bars("CALM"), p)
    assert bad is None, "quiet/illiquid name must be filtered out"
    print("PASS: quiet/illiquid name filtered out")

    # Too little history → None (no crash).
    assert score_symbol(trending_daily_bars("SHORT", n=10), p) is None
    print("PASS: insufficient history → None")

    # build_watchlist ranks and drops failures.
    fixtures = {
        "MOVR": trending_daily_bars("MOVR"),
        "CALM": quiet_daily_bars("CALM"),
        "FAST": trending_daily_bars("FAST"),
    }

    async def fetch(sym):
        return [dict(b) for b in fixtures[sym]]

    watchlist = asyncio.run(build_watchlist(list(fixtures), fetch, p))
    syms = [c.symbol for c in watchlist]
    assert "CALM" not in syms, syms
    assert set(syms) == {"MOVR", "FAST"}, syms
    # Ranked best-first by score.
    assert watchlist == sorted(watchlist, key=lambda c: c.score, reverse=True)
    print(f"PASS: build_watchlist ranks + filters → {syms}")

    print("\nALL EMA SCANNER TESTS PASSED")


if __name__ == "__main__":
    _run()
