"""Strategy hooks expose (symbol, interval) specs + bar interval.
   Run: python -m tests.test_backfill_specs
"""
from __future__ import annotations


def _run() -> None:
    from app.strategies.base import BaseStrategy

    # default base behaviour
    class Bare(BaseStrategy):
        key = "k"
        name = "n"
        async def scan(self, universe):
            return []
    b = Bare()
    assert b.backfill_specs("AAPL") == [] and b.bar_interval() == "5m"

    from app.strategies.ema_scalper.strategy import EmaScalperStrategy
    s = EmaScalperStrategy()
    assert s.bar_interval() == s.params.candle_interval
    specs = s.backfill_specs("AAPL")
    assert specs == [("AAPL", s.params.candle_interval)], specs
    assert s.backfill_specs(None) == []
    print("PASS: strategy resilience hooks")
    print("\nALL BACKFILL-SPECS TESTS PASSED")


if __name__ == "__main__":
    _run()
