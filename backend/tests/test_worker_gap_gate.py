"""Entries are blocked on a stale series when the market is open; fresh bars are not.
   Run: python -m tests.test_worker_gap_gate
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.engine import worker as W

        class Strat:
            key = "strategy_1"
            def bar_interval(self): return "5m"

        now_ms = __import__("time").time() * 1000
        stale_bars = [{"time": int(now_ms - 3_600_000)}]   # 1h old
        fresh_bars = [{"time": int(now_ms - 30_000)}]      # 30s old

        import app.services.market as mkt
        mkt.get_market_status = lambda: {"is_open": True}   # type: ignore[assignment]

        assert await W._entry_blocked_by_gap(Strat(), stale_bars) is True
        assert await W._entry_blocked_by_gap(Strat(), fresh_bars) is False

        mkt.get_market_status = lambda: {"is_open": False}  # type: ignore[assignment]
        assert await W._entry_blocked_by_gap(Strat(), stale_bars) is False  # closed → allowed
        print("PASS: entry gap gate")
    asyncio.run(_t())
    print("\nALL WORKER-GAP-GATE TESTS PASSED")


if __name__ == "__main__":
    _run()
