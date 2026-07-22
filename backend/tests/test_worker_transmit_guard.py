"""Worker must NOT open a position, consume risk budget, or write an open trade
record when an order genuinely fails to transmit (dry-run reject / broker
unavailable). It MUST still open normally in paper mode, where ``transmitted``
is False but ``paper`` is True. Also: when a live bracket's fill price can't be
determined, the worker must never fabricate the stop as the fill price.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_tg.db python -m pytest tests/test_worker_transmit_guard.py
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import TradeRecord
from app.engine import worker as W
from app.engine.risk import RiskEngine, RiskLimits
from app.strategies.ema_crossover.signals import Action, State


class _Strat:
    key = "guard_test"
    name = "Guard Test"

    async def _fetch_1m(self, symbol):
        return []


def _bars(t=1_700_000_000_000):
    return [{"time": t, "open": 100.0, "high": 100.5, "low": 99.5,
             "close": 100.0, "volume": 1000, "symbol": "AAA"}]


def _engine() -> RiskEngine:
    return RiskEngine(10_000.0, RiskLimits.from_settings())


@pytest.mark.asyncio
async def test_open_bracket_genuine_failure_opens_nothing(monkeypatch):
    """broker unavailable -> transmitted False, NOT paper -> no position, no risk, no trade."""
    await init_db()
    async def failing(*a, **k):
        return {"transmitted": False, "detail": "broker unavailable (no TT session)"}
    monkeypatch.setattr("app.engine.execution.submit_bracket_order", failing)

    eng = _engine()
    state = State()
    action = Action("enter", "long", 100.0, stop=98.0, target=104.0)
    track = await W._open_bracket(_Strat(), "AAA", action, state, _bars(), eng)

    assert track is None, "must not return a tracking dict on genuine failure"
    assert state.position is None, "must not set a position on genuine failure"
    assert eng.open_count == 0 and not eng.has_position("AAA"), "must not consume risk budget"
    async with session_scope() as s:
        rows = (await s.execute(select(TradeRecord).where(
            TradeRecord.strategy_key == "guard_test"))).scalars().all()
    assert rows == [], "must not write an open trade record on genuine failure"


@pytest.mark.asyncio
async def test_open_bracket_paper_still_opens(monkeypatch):
    """Paper mode: transmitted False but paper True -> normal open (position + trade)."""
    await init_db()
    async def paper(*a, **k):
        return {"transmitted": False, "paper": True, "bracket": True,
                "detail": "paper bracket long AAA x50 (stop 98.00 / target 104.00)",
                "stop": 98.0, "target": 104.0}
    monkeypatch.setattr("app.engine.execution.submit_bracket_order", paper)

    eng = _engine()
    state = State()
    action = Action("enter", "long", 100.0, stop=98.0, target=104.0)
    track = await W._open_bracket(_Strat(), "AAA", action, state, _bars(), eng)

    assert track is not None and track["qty"] == 50, track
    assert state.position is not None, "paper mode must set the position"
    assert eng.has_position("AAA"), "paper mode must register the open in the risk engine"
    async with session_scope() as s:
        row = (await s.execute(select(TradeRecord).where(
            TradeRecord.strategy_key == "guard_test",
            TradeRecord.status == "open"))).scalars().first()
    assert row is not None and row.quantity == 50, "paper mode must record the open trade"


@pytest.mark.asyncio
async def test_reconcile_open_never_books_stop_on_undetermined_fill(monkeypatch):
    """Live: get_order_history raised -> poll returns price None. The worker must NOT
    finalize the exit at the stop price (fabricated worst-case loss)."""
    await init_db()
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)

    async def undetermined(symbol, ref=None):
        return {"price": None, "ms": None, "reason": "error"}
    monkeypatch.setattr("app.engine.execution.poll_bracket_fill", undetermined)

    finalized = []
    async def spy_finalize(*a, **k):
        finalized.append((a, k))
    monkeypatch.setattr(W, "_finalize_exit", spy_finalize)

    eng = _engine()
    state = State()
    # Give it an open position so the live reconcile path runs.
    from app.strategies.ema_crossover.signals import Position
    state.position = Position(side="long", entry_price=100.0, stop=98.0, target=104.0,
                             entry_index=0, entry_time=1_700_000_000_000)

    closed = await W._reconcile_open(_Strat(), "AAA", state, eng, {"broker_id": "1"})

    assert closed is False, "an undetermined fill must not be treated as a close"
    assert finalized == [], "must not finalize (book P&L) on an undetermined fill price"
    assert state.position is not None, "position must remain open when the fill is undetermined"


@pytest.mark.asyncio
async def test_reconcile_open_flat_but_unmatched_escalates_safe_mode(monkeypatch):
    """Live: broker shows flat but no matching fill found (price None, reason 'bracket').
    The exit price is genuinely unknown -> escalate to SAFE_MODE rather than fabricate a
    fill or silently loop forever. Still must not finalize at the stop."""
    await init_db()
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)

    async def unmatched(symbol, ref=None):
        return {"price": None, "ms": None, "reason": "bracket"}
    monkeypatch.setattr("app.engine.execution.poll_bracket_fill", unmatched)

    finalized = []
    async def spy_finalize(*a, **k):
        finalized.append((a, k))
    monkeypatch.setattr(W, "_finalize_exit", spy_finalize)

    from app.engine import safe_mode as SM
    assert await SM.is_safe_mode() is False

    eng = _engine()
    state = State()
    from app.strategies.ema_crossover.signals import Position
    state.position = Position(side="long", entry_price=100.0, stop=98.0, target=104.0,
                             entry_index=0, entry_time=1_700_000_000_000)

    closed = await W._reconcile_open(_Strat(), "AAA", state, eng, {"broker_id": "1"})

    assert closed is False and finalized == [], "must not fabricate a fill"
    assert await SM.is_safe_mode() is True, "an unresolvable broker/local mismatch must trip SAFE_MODE"
