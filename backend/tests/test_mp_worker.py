"""Multi-position worker loop test.

Run: DATABASE_URL=sqlite+aiosqlite:///./_test_mp.db python -m tests.test_mp_worker

Drives a scripted multi-position strategy through the real ``_run_multi_position``
loop (paper mode, no broker): it opens a risk-sized BRACKET position, records an
open trade, then the paper bracket resolves the target on injected 1m bars and
closes it with P&L — and the kill switch cancels cleanly. Also checks the worker
reconstructs an existing open position (+ its tracking) on restart.
"""

from __future__ import annotations

import asyncio


async def _run() -> None:
    from app.config import get_settings
    from app.db.base import init_db, session_scope
    from app.db.models import StrategyState, TradeRecord
    from app.engine.manager import engine_manager
    from app.engine.worker import _reconstruct_positions, run_strategy
    from app.engine.risk import RiskEngine, RiskLimits
    from app.strategies.base import BaseStrategy
    from app.strategies.ema_crossover.signals import Action
    from sqlalchemy import select

    settings = get_settings()
    settings.scan_interval_seconds = 0          # tight loop
    settings.watchlist = "AAA"                  # single-symbol watchlist for the test

    from app.engine import worker as worker_mod
    worker_mod.broker_ready = lambda: True    # workers only act when ready
    import app.services.market as _mkt
    _mkt.get_market_status = lambda: {"is_open": False}  # closed → gap gate is pass-through

    await init_db()
    async with session_scope() as s:
        s.add(StrategyState(key="mp_test", name="MP Test", ready=True, status="idle",
                            capital_allocation=10_000.0))

    _T = 1_700_000_000_000

    class FakeMP(BaseStrategy):
        key = "mp_test"
        name = "MP Test"
        ready = True
        is_rule_driven = True
        is_multi_position = True

        def __init__(self) -> None:
            self.fill_1m: list = []  # set to "arm" the paper bracket's target fill

        async def scan(self, universe):  # pragma: no cover - unused in mp loop
            return []

        async def live_decision(self, symbol, state, direction="both"):
            bars = [{"time": _T, "open": 100.0, "high": 100.3, "low": 99.7,
                     "close": 100.0, "volume": 1000, "symbol": symbol}]
            # Flat → bracketed long entry; while holding → no 5m signal (the resting
            # bracket / 1m sim owns the exit).
            if state.position is None:
                return Action("enter", "long", 100.0, stop=98.0, target=104.0), bars
            return Action("none"), bars

        async def _fetch_1m(self, symbol):
            return self.fill_1m

    fake = FakeMP()
    await engine_manager.start_worker("mp_test", run_strategy(fake))

    # 1) An open trade should appear (risk-sized: 1% of 10k = $100 risk / $2 = 50 shares).
    opened = None
    for _ in range(100):
        await asyncio.sleep(0.02)
        async with session_scope() as s:
            opened = (await s.execute(
                select(TradeRecord).where(TradeRecord.strategy_key == "mp_test",
                                          TradeRecord.status == "open"))).scalars().first()
            if opened:
                qty, entry, stop = opened.quantity, opened.entry_price, opened.stop
                break
    assert opened is not None, "expected an open trade"
    assert qty == 50, f"risk-sized quantity should be 50, got {qty}"
    assert entry == 100.0 and stop == 98.0, (entry, stop)
    print(f"PASS: opened risk-sized bracket long x{qty} @ {entry} (stop {stop}, target 104)")

    # 2) Arm the paper bracket: a 1m bar (after entry) that hits the target → the
    # worker resolves it via resolve_intrabar_exit and closes at 104 ((104-100)*50=200).
    fake.fill_1m = [{"time": _T + 60_000, "open": 104.0, "high": 104.5, "low": 103.6,
                     "close": 104.2, "volume": 100, "symbol": "AAA"}]
    closed = None
    for _ in range(100):
        await asyncio.sleep(0.02)
        async with session_scope() as s:
            closed = (await s.execute(
                select(TradeRecord).where(TradeRecord.strategy_key == "mp_test",
                                          TradeRecord.status == "closed"))).scalars().first()
            if closed:
                pnl, exitp = closed.pnl, closed.exit_price
                break
    assert closed is not None, "expected the trade to close"
    assert exitp == 104.0 and abs(pnl - 200.0) < 1e-6, (exitp, pnl)
    print(f"PASS: paper bracket filled target @ {exitp} pnl={pnl}")

    # 3) Kill switch cancels the worker cleanly.
    report = await engine_manager.kill_all()
    assert "mp_test" in report.tasks_cancelled, report.to_dict()
    assert not engine_manager.is_running("mp_test")
    print("PASS: multi-position worker cancelled cleanly by kill switch")

    # 4) Restart reconstruction: open a fresh position in the DB, rebuild it.
    await asyncio.sleep(0.4)  # let the cancelled worker's DB connection fully release
    async with session_scope() as s:
        from datetime import datetime, timezone
        s.add(TradeRecord(strategy_key="mp_test", symbol="ZZZ", side="buy", quantity=10,
                          entry_price=200.0, stop=196.0, target=208.0, status="open",
                          entry_time=datetime.now(timezone.utc)))
    eng = RiskEngine(10_000.0, RiskLimits.from_settings())
    states, track = await _reconstruct_positions("mp_test", eng)
    assert "ZZZ" in states and states["ZZZ"].position is not None, states
    assert track["ZZZ"]["qty"] == 10 and track["ZZZ"]["trade_id"] is not None, track
    assert eng.open_count == 1 and eng.has_position("ZZZ"), eng.snapshot()
    # The reconstructed entry was opened "today" → it counts toward today's cap.
    assert eng.trades_today == 1, eng.trades_today
    print("PASS: restart reconstructs the open position + tracking + risk state")

    print("\nALL MULTI-POSITION WORKER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
