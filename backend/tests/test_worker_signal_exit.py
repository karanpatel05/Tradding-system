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
    """order_result may be a dict or a list of dicts (one per submit call)."""
    calls = {"cancel": 0, "submit": 0, "refs": []}
    results = list(order_result) if isinstance(order_result, list) else None

    async def cancel(symbol):
        calls["cancel"] += 1
        return cancel_result

    async def submit(symbol, intent, qty, *, reason="", ref=None):
        calls["submit"] += 1
        calls["refs"].append(ref)
        return results.pop(0) if results is not None else order_result
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


# ---------------------------------------------------- held exit retried next cycle
DEAD = {"price": None, "ms": None, "reason": "bracket"}
UNKNOWN = {"transmitted": False, "ref": "r-exit-1", "unknown_state": True,
           "detail": "Alpaca request failed (ReadTimeout); order state unknown, ref r-exit-1"}
OK = {"transmitted": True, "order_id": "alp-9", "ref": "r-exit-1", "detail": "LIVE SELL"}


def _poll(monkeypatch, *results):
    seq = list(results)

    async def poll(symbol, ref=None):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr("app.engine.execution.poll_bracket_fill", poll)


async def _cycle(sym, eng, state, track, action):
    await W._manage_open(_Strat(), sym, state, eng, track, action, _bars(),
                         _bars()[-1]["time"] if action else None)


@pytest.mark.asyncio
async def test_held_exit_is_retried_before_dead_bracket_trips_safe_mode(monkeypatch):
    """Cycle 1: signal exit, cancel OK, close POST times out (state unknown) -> held.
    Cycle 2: the bracket legs are now canceled so the poll says 'bracket' (dead). That
    must NOT trip SAFE_MODE while the exit is pending: the exit is retried with the SAME
    ref (no duplicate sell) and, on success, finalized."""
    from app.engine import safe_mode as SM
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XRETRY")
    await SM.clear_safe_mode()
    track = {"XRETRY": tr}
    calls = _wire(monkeypatch, {"cancelled": 2}, [UNKNOWN, OK])
    _poll(monkeypatch, None, DEAD)

    await _cycle("XRETRY", eng, state, track, _exit_action())
    assert track["XRETRY"]["exit_pending"] is True and calls["submit"] == 1
    await _assert_still_open("XRETRY", eng, state, tr)

    await _cycle("XRETRY", eng, state, track, None)        # no fresh signal this cycle
    assert await SM.is_safe_mode() is False
    assert calls["submit"] == 2 and calls["refs"] == [None, "r-exit-1"]
    assert state.position is None and "XRETRY" not in track
    assert await _status(tr["trade_id"]) == "closed"


@pytest.mark.asyncio
async def test_held_exit_retry_failing_again_stays_open_no_safe_mode(monkeypatch):
    from app.engine import safe_mode as SM
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XRETRY2")
    await SM.clear_safe_mode()
    track = {"XRETRY2": tr}
    rejected = {"transmitted": False, "ref": "r-exit-1",
                "detail": "rejected by Alpaca: insufficient qty"}
    calls = _wire(monkeypatch, {"cancelled": 2}, [UNKNOWN, rejected, OK])
    _poll(monkeypatch, None, DEAD)

    await _cycle("XRETRY2", eng, state, track, _exit_action())
    await _cycle("XRETRY2", eng, state, track, None)
    assert await SM.is_safe_mode() is False
    await _assert_still_open("XRETRY2", eng, state, tr)
    assert track["XRETRY2"]["exit_pending"] is True
    # a definitive rejection drops the ref: the next retry is a fresh order, not a dupe
    await _cycle("XRETRY2", eng, state, track, None)
    assert calls["refs"] == [None, "r-exit-1", None]
    assert await _status(tr["trade_id"]) == "closed"


@pytest.mark.asyncio
async def test_pending_exit_still_honours_a_real_bracket_fill(monkeypatch):
    """Cancel was still pending (legs live) and the stop filled meanwhile: book that fill,
    don't send a second close."""
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XSTOP")
    track = {"XSTOP": tr}
    calls = _wire(monkeypatch, {"cancelled": 1, "pending": True}, OK)
    _poll(monkeypatch, None, {"price": 98.0, "ms": T0 + 600_000, "reason": "stop"})
    await _cycle("XSTOP", eng, state, track, _exit_action())
    assert calls["submit"] == 0 and track["XSTOP"]["exit_pending"] is True
    await _cycle("XSTOP", eng, state, track, None)
    assert calls["submit"] == 0 and state.position is None and "XSTOP" not in track
    assert await _status(tr["trade_id"]) == "closed"
