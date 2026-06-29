"""Multi-position worker: TWO concurrent symbols sharing one buying-power pool.

Run: DATABASE_URL=sqlite+aiosqlite:///./_test_mpm.db python -m tests.test_mp_multi

Proves the headline Phase-3 behavior end-to-end: two watchlist symbols open
positions at once, and the SECOND entry's size is capped by the cash the FIRST
already consumed (not just by its own risk budget) — i.e. the shared pool is
accounted across symbols inside the live loop, then both close.
"""

from __future__ import annotations

import asyncio


async def _run() -> None:
    from app.config import get_settings
    from app.db.base import init_db, session_scope
    from app.db.models import StrategyState, TradeRecord
    from app.engine.manager import engine_manager
    from app.engine.worker import run_strategy
    from app.strategies.base import BaseStrategy
    from app.strategies.ema_crossover.signals import Action
    from sqlalchemy import select

    settings = get_settings()
    settings.scan_interval_seconds = 0
    settings.watchlist = "AAA,BBB"            # two symbols, processed in this order
    # Defaults: risk 1%, max 5 positions, portfolio risk 5%. Allocation 5000 -> $50
    # risk budget/trade. AAA (per-share risk 2) sizes 25 sh = $2500 notional, leaving
    # $2500. BBB (per-share risk 0.5) would risk-size 100 sh ($10k) but the remaining
    # $2500 pool caps it to 25 sh — that cap is the thing under test.
    alloc = 5000.0

    from app.ibkr.gateway import ConnState, get_gateway
    get_gateway().set_state(ConnState.READY)    # workers only act when READY
    import app.services.market as _mkt
    _mkt.get_market_status = lambda: {"is_open": False}  # closed → gap gate is pass-through

    await init_db()
    async with session_scope() as s:
        s.add(StrategyState(key="mpm", name="MPM", ready=True, status="idle",
                            capital_allocation=alloc))

    _T = 1_700_000_000_000

    class FakeMP(BaseStrategy):
        key = "mpm"
        name = "MPM"
        ready = True
        is_rule_driven = True
        is_multi_position = True

        def __init__(self) -> None:
            self.arm = False  # flip to release the paper bracket target fills

        async def scan(self, universe):  # pragma: no cover
            return []

        async def live_decision(self, symbol, state, direction="both"):
            bars = [{"time": _T, "open": 100.0, "high": 100.3, "low": 99.7,
                     "close": 100.0, "volume": 1000, "symbol": symbol}]
            stop = 98.0 if symbol == "AAA" else 99.5  # AAA risk 2/sh, BBB 0.5/sh
            if state.position is None:
                return Action("enter", "long", 100.0, stop=stop, target=104.0), bars
            return Action("none"), bars

        async def _fetch_1m(self, symbol):
            if not self.arm:
                return []
            return [{"time": _T + 60_000, "open": 104.0, "high": 104.5, "low": 103.6,
                     "close": 104.2, "volume": 100, "symbol": symbol}]  # hits target

    fake = FakeMP()
    await engine_manager.start_worker("mpm", run_strategy(fake))

    # 1) Both symbols open concurrently.
    by_symbol: dict[str, TradeRecord] = {}
    for _ in range(200):
        await asyncio.sleep(0.02)
        async with session_scope() as s:
            rows = (await s.execute(
                select(TradeRecord).where(TradeRecord.strategy_key == "mpm",
                                          TradeRecord.status == "open"))).scalars().all()
            by_symbol = {r.symbol: (r.quantity, r.entry_price, r.stop) for r in rows}
        if {"AAA", "BBB"} <= set(by_symbol):
            break
    assert {"AAA", "BBB"} <= set(by_symbol), f"expected both open, got {by_symbol}"
    aaa_qty = by_symbol["AAA"][0]
    bbb_qty = by_symbol["BBB"][0]
    assert aaa_qty == 25, f"AAA risk-sized qty should be 25, got {aaa_qty}"
    # BBB's own risk budget would buy 100 shares; the shared pool caps it to 25.
    assert bbb_qty == 25, f"BBB should be cash-pool-capped to 25, got {bbb_qty}"
    print(f"PASS: two concurrent positions; BBB cash-pool-capped to {bbb_qty} (risk budget alone = 100)")

    # 2) Arm the paper brackets (1m bar hits the target); both should close.
    fake.arm = True
    closed: set[str] = set()
    for _ in range(200):
        await asyncio.sleep(0.02)
        async with session_scope() as s:
            rows = (await s.execute(
                select(TradeRecord).where(TradeRecord.strategy_key == "mpm",
                                          TradeRecord.status == "closed"))).scalars().all()
            closed = {r.symbol for r in rows}
        if {"AAA", "BBB"} <= closed:
            break
    assert {"AAA", "BBB"} <= closed, f"expected both closed, got {closed}"
    print("PASS: both positions closed")

    report = await engine_manager.kill_all()
    assert "mpm" in report.tasks_cancelled, report.to_dict()
    print("PASS: worker cancelled cleanly")

    print("\nALL MULTI-SYMBOL POOL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
