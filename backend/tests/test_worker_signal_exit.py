"""Signal/EOD exit must never book a phantom close: when live (gate ON) and the resting
bracket cancel is still pending / blocked, or the market exit genuinely fails to transmit,
the trade stays open (state, risk engine and TradeRecord untouched) so the next cycle
retries. Paper (gate OFF) is unchanged. Only app.engine.execution functions are faked."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import TradeRecord
from app.engine import worker as W
from app.engine.risk import RiskEngine, RiskLimits
from app.strategies.ema_crossover.signals import Action, Position, State

T0 = 1_700_000_000_000


class _Strat:
    key = "exit_guard_test"
    name = "Exit Guard Test"


async def _setup(symbol: str):
    await init_db()
    eng = RiskEngine(10_000.0, RiskLimits.from_settings())
    eng.register_open(symbol, 10, 100.0, 98.0)
    state = State()
    state.position = Position(side="long", entry_price=100.0, stop=98.0, target=104.0,
                              entry_index=0, entry_time=T0)
    trade_id = await W._open_trade(_Strat.key, symbol, "long", 10, 100.0, T0, 98.0, 104.0,
                                   broker_order_id="par1")
    tr = {"trade_id": trade_id, "qty": 10, "broker_id": "par1"}
    return eng, state, tr


async def _status(trade_id: int) -> str:
    async with session_scope() as s:
        return (await s.execute(select(TradeRecord.status).where(
            TradeRecord.id == trade_id))).scalar_one()


def _wire(monkeypatch, cancel_result, order_result):
    calls = {"cancel": 0, "submit": 0}

    async def cancel(symbol):
        calls["cancel"] += 1
        return cancel_result

    async def submit(symbol, intent, qty, *, reason=""):
        calls["submit"] += 1
        return order_result
    monkeypatch.setattr("app.engine.execution.cancel_symbol_orders", cancel)
    monkeypatch.setattr("app.engine.execution.submit_equity_order", submit)
    return calls


def _exit_action():
    return Action("exit", "long", 101.0, reason="opposite_cross")


def _bars():
    return [{"time": T0 + 300_000, "close": 101.0}]


async def _assert_still_open(symbol, eng, state, tr):
    assert state.position is not None, "position must stay open for the retry"
    assert eng.has_position(symbol), "risk budget must not be released"
    assert await _status(tr["trade_id"]) == "open", "TradeRecord must stay open"


@pytest.mark.asyncio
async def test_live_cancel_pending_does_not_exit(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XPEND")
    calls = _wire(monkeypatch, {"cancelled": 1, "pending": True},
                  {"transmitted": True, "order_id": "x", "detail": "LIVE SELL"})
    done = await W._exit_via_signal(_Strat(), "XPEND", state, eng, tr, _exit_action(), _bars())
    assert done is False
    assert calls["submit"] == 0, "must not market-close while bracket legs still reserve shares"
    await _assert_still_open("XPEND", eng, state, tr)


@pytest.mark.asyncio
async def test_live_cancel_blocked_does_not_exit(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XBLK")
    calls = _wire(monkeypatch,
                  {"cancelled": 0, "detail": "blocked: Alpaca live endpoint locked (paper only)"},
                  {"transmitted": True, "order_id": "x", "detail": "LIVE SELL"})
    done = await W._exit_via_signal(_Strat(), "XBLK", state, eng, tr, _exit_action(), _bars())
    assert done is False and calls["submit"] == 0
    await _assert_still_open("XBLK", eng, state, tr)


@pytest.mark.asyncio
async def test_live_exit_order_failure_keeps_trade_open(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XFAIL")
    calls = _wire(monkeypatch, {"cancelled": 1},
                  {"transmitted": False, "ref": "r",
                   "detail": "Alpaca request failed (ReadTimeout); order state unknown, ref r"})
    done = await W._exit_via_signal(_Strat(), "XFAIL", state, eng, tr, _exit_action(), _bars())
    assert done is False and calls["submit"] == 1
    await _assert_still_open("XFAIL", eng, state, tr)


@pytest.mark.asyncio
async def test_live_exit_success_finalizes(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XOK")
    _wire(monkeypatch, {"cancelled": 2},
          {"transmitted": True, "order_id": "x", "ref": "r", "detail": "LIVE SELL XOK x10"})
    done = await W._exit_via_signal(_Strat(), "XOK", state, eng, tr, _exit_action(), _bars())
    assert done is True
    assert state.position is None and not eng.has_position("XOK")
    assert await _status(tr["trade_id"]) == "closed"


@pytest.mark.asyncio
async def test_paper_exit_unchanged(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    eng, state, tr = await _setup("XPAP")
    _wire(monkeypatch, {"cancelled": 0, "paper": True},
          {"transmitted": False, "paper": True, "fill_price": 101.0,
           "detail": "paper SELL XPAP x10 @ 101.00"})
    done = await W._exit_via_signal(_Strat(), "XPAP", state, eng, tr, _exit_action(), _bars())
    assert done is True
    assert state.position is None and await _status(tr["trade_id"]) == "closed"
