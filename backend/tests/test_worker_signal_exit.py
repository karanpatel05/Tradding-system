"""Signal/EOD exit must never book a phantom close: when live (gate ON) and the resting
bracket cancel is still pending / blocked, or the market exit genuinely fails to transmit,
the trade stays open (state, risk engine and TradeRecord untouched) so the next cycle
retries. Paper (gate OFF) is unchanged. Only app.engine.execution functions and the app
ledger are faked; any real Alpaca HTTP call fails the test."""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import TradeRecord
from app.engine import worker as W
from app.engine.risk import RiskEngine, RiskLimits
from app.strategies.ema_crossover.signals import Action, Position, State

T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("worker test touched the network")
    monkeypatch.setattr("app.broker.alpaca_client.httpx.AsyncClient", boom)


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
    tr = {"trade_id": trade_id, "qty": 10, "broker_id": "par1", "entry_ms": T0}
    return eng, state, tr


async def _status(trade_id: int) -> str:
    async with session_scope() as s:
        return (await s.execute(select(TradeRecord.status).where(
            TradeRecord.id == trade_id))).scalar_one()


class _AppHolds(dict):
    """Fake app ledger: the app still holds the trade's 10 shares unless told otherwise."""

    def get(self, k, default=None):
        return super().get(k, 10)


DEAD = {"price": None, "ms": None, "reason": "bracket"}


def _wire(monkeypatch, cancel_result, order_result, net=None):
    """order_result may be a dict or a list of dicts (one per submit call). After a clean
    cancel the post-cancel poll sees a dead bracket (our own cancel) and the app ledger
    holds the trade's shares (`net` overrides; an Exception instance is raised)."""
    calls = {"cancel": 0, "submit": 0, "refs": [], "qtys": [], "poll": [], "ledger": 0}
    results = list(order_result) if isinstance(order_result, list) else None

    async def cancel(symbol):
        calls["cancel"] += 1
        return cancel_result

    async def submit(symbol, intent, qty, *, reason="", ref=None):
        calls["submit"] += 1
        calls["refs"].append(ref)
        calls["qtys"].append(qty)
        return results.pop(0) if results is not None else order_result

    async def poll(symbol, ref=None, **kw):
        calls["poll"].append((symbol, ref, kw))
        return DEAD

    async def ledger():
        calls["ledger"] += 1
        if isinstance(net, Exception):
            raise net
        return _AppHolds() if net is None else net
    monkeypatch.setattr("app.engine.execution.cancel_symbol_orders", cancel)
    monkeypatch.setattr("app.engine.execution.submit_equity_order", submit)
    monkeypatch.setattr("app.engine.execution.poll_bracket_fill", poll)
    monkeypatch.setattr("app.broker.ledger.app_net_positions", ledger)
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
    calls = _wire(monkeypatch, {"cancelled": 0, "paper": True},
                  {"transmitted": False, "paper": True, "fill_price": 101.0,
                   "detail": "paper SELL XPAP x10 @ 101.00"})
    done = await W._exit_via_signal(_Strat(), "XPAP", state, eng, tr, _exit_action(), _bars())
    assert done is True
    assert state.position is None and await _status(tr["trade_id"]) == "closed"
    assert calls["poll"] == [] and calls["ledger"] == 0, "gate OFF: no broker reads"


# ---------------------------------------------------- held exit retried next cycle
UNKNOWN = {"transmitted": False, "ref": "r-exit-1", "unknown_state": True,
           "detail": "Alpaca request failed (ReadTimeout); order state unknown, ref r-exit-1"}
OK = {"transmitted": True, "order_id": "alp-9", "ref": "r-exit-1", "detail": "LIVE SELL"}


def _poll(monkeypatch, *results):
    """Every poll (the reconcile poll and the post-cancel re-poll) consumes the sequence;
    the last result repeats."""
    seq = list(results)
    seen = []

    async def poll(symbol, ref=None, **kw):
        seen.append((symbol, ref, kw))
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr("app.engine.execution.poll_bracket_fill", poll)
    return seen


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


async def _trade(trade_id: int) -> TradeRecord:
    async with session_scope() as s:
        return await s.get(TradeRecord, trade_id)


# ---------------------------------------------------- I3a: unpriced stop/target fill
@pytest.mark.asyncio
async def test_pending_exit_unpriced_stop_fill_is_never_resold(monkeypatch):
    """A held exit must only ignore a DEAD bracket (our own cancel). A stop/target that
    actually filled with no filled_avg_price is a real exit: never retry the close (that
    would sell twice) — escalate to SAFE_MODE and leave the trade for a human."""
    from app.engine import safe_mode as SM
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XNOPX")
    await SM.clear_safe_mode()
    tr.update(exit_pending=True, exit_action=_exit_action())
    track = {"XNOPX": tr}
    calls = _wire(monkeypatch, {"cancelled": 0}, OK)
    _poll(monkeypatch, {"price": None, "ms": T0 + 600_000, "reason": "stop"})
    try:
        await _cycle("XNOPX", eng, state, track, None)
        assert calls["submit"] == 0, "an exit child filled: no second close"
        assert await SM.is_safe_mode() is True
        await _assert_still_open("XNOPX", eng, state, tr)
    finally:
        await SM.clear_safe_mode()


# ---------------------------------------------------- I4: race with the bracket
@pytest.mark.asyncio
async def test_signal_exit_books_bracket_fill_that_beat_the_cancel(monkeypatch):
    """The poll saw live exits, but the target filled before our cancel landed (cancel
    finds nothing and returns clean). The re-poll after the cancel sees the fill: book it,
    send no market close."""
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XRACE")
    calls = _wire(monkeypatch, {"cancelled": 0}, OK)
    seen = _poll(monkeypatch, {"price": 104.0, "ms": T0 + 600_000, "reason": "target"})
    done = await W._exit_via_signal(_Strat(), "XRACE", state, eng, tr, _exit_action(), _bars())
    assert done is True and calls["submit"] == 0
    assert seen == [("XRACE", "par1", {"since_ms": T0})]
    row = await _trade(tr["trade_id"])
    assert row.status == "closed" and row.exit_price == 104.0 and row.pnl == pytest.approx(40.0)
    assert state.position is None and not eng.has_position("XRACE")


@pytest.mark.asyncio
async def test_signal_exit_close_is_capped_at_app_ledger_qty(monkeypatch):
    """Entry partially filled (4 of 10) then the rest was cancelled: close only the 4 the
    app actually holds, and book P&L on 4."""
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XPART")
    calls = _wire(monkeypatch, {"cancelled": 2}, OK, net={"XPART": 4})
    done = await W._exit_via_signal(_Strat(), "XPART", state, eng, tr, _exit_action(), _bars())
    assert done is True and calls["qtys"] == [4]
    row = await _trade(tr["trade_id"])
    assert row.status == "closed" and row.pnl == pytest.approx(4.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("net", [{}, {"XFLAT": -5}])
async def test_signal_exit_with_nothing_held_sends_no_order(monkeypatch, net):
    """The app ledger holds nothing in the trade's direction (exit already filled, or the
    app is net the other way): close the record with unknown P&L, transmit nothing."""
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XFLAT")
    calls = _wire(monkeypatch, {"cancelled": 0}, OK, net=net)
    done = await W._exit_via_signal(_Strat(), "XFLAT", state, eng, tr, _exit_action(), _bars())
    assert done is True and calls["submit"] == 0
    row = await _trade(tr["trade_id"])
    assert row.status == "closed" and row.pnl is None and row.exit_price is None
    assert state.position is None and not eng.has_position("XFLAT")


@pytest.mark.asyncio
async def test_signal_exit_holds_when_repoll_errors(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XPERR")
    calls = _wire(monkeypatch, {"cancelled": 0}, OK)
    _poll(monkeypatch, {"price": None, "ms": None, "reason": "error"})
    done = await W._exit_via_signal(_Strat(), "XPERR", state, eng, tr, _exit_action(), _bars())
    assert done is False and calls["submit"] == 0 and tr["exit_pending"] is True
    await _assert_still_open("XPERR", eng, state, tr)


@pytest.mark.asyncio
async def test_signal_exit_holds_when_ledger_unreadable(monkeypatch):
    from app.broker.alpaca_client import AlpacaError
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XLERR")
    calls = _wire(monkeypatch, {"cancelled": 0}, OK, net=AlpacaError(503, "down"))
    done = await W._exit_via_signal(_Strat(), "XLERR", state, eng, tr, _exit_action(), _bars())
    assert done is False and calls["submit"] == 0 and tr["exit_pending"] is True
    await _assert_still_open("XLERR", eng, state, tr)


@pytest.mark.asyncio
async def test_signal_exit_unpriced_fill_after_cancel_holds_and_trips_safe_mode(monkeypatch):
    from app.engine import safe_mode as SM
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XUNP")
    await SM.clear_safe_mode()
    calls = _wire(monkeypatch, {"cancelled": 0}, OK)
    _poll(monkeypatch, {"price": None, "ms": None, "reason": "target"})
    try:
        done = await W._exit_via_signal(_Strat(), "XUNP", state, eng, tr, _exit_action(),
                                        _bars())
        assert done is False and calls["submit"] == 0
        assert await SM.is_safe_mode() is True
        await _assert_still_open("XUNP", eng, state, tr)
    finally:
        await SM.clear_safe_mode()


# ---------------------------------------------------- I5c: poll scoped to the trade
@pytest.mark.asyncio
async def test_reconcile_poll_is_scoped_to_trade_entry(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    eng, state, tr = await _setup("XSCOPE")
    seen = _poll(monkeypatch, None)
    assert await W._reconcile_open(_Strat(), "XSCOPE", state, eng, tr) is False
    assert seen == [("XSCOPE", "par1", {"since_ms": T0})]


# ---------------------------------------------------- I5b: rule-driven exit
@pytest.mark.asyncio
async def test_rule_driven_live_exit_failure_keeps_trade_open(monkeypatch):
    """_handle_action exit path: a genuine live transmit failure must not book the exit
    (mirrors _exit_via_signal) — the position and TradeRecord stay open for a retry."""
    from app.strategies.ema_scalper.signals import Action as SAction
    from app.strategies.ema_scalper.signals import Position as SPosition
    from app.strategies.ema_scalper.signals import State as SState
    from app.strategies.ema_scalper.signals import apply_action as s_apply

    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    await init_db()
    trade_id = await W._open_trade(_Strat.key, "XRULE", "long", 10, 100.0, T0, 98.0, 104.0,
                                   transmitted=True)
    state = SState(position=SPosition("long", 100.0, 98.0, 104.0, 0, entry_time=T0))
    sent = []

    async def failing(symbol, intent, qty, *, reason="", ref=None):
        sent.append((symbol, intent, qty))
        return {"transmitted": False, "ref": "r1",
                "detail": "rejected by Alpaca: insufficient qty"}
    action = SAction("exit", "long", 101.0, reason="target")
    bars = [{"time": T0 + 300_000, "high": 101.5, "low": 100.5, "close": 101.0}]
    qty, tid = await W._handle_action(_Strat(), "XRULE", action, state, bars, 10, trade_id,
                                      size_position=None, submit_equity_order=failing,
                                      apply_action=s_apply)
    assert sent == [("XRULE", "exit_long", 10)]
    assert (qty, tid) == (10, trade_id), "must keep tracking the open trade"
    assert state.position is not None
    assert await _status(trade_id) == "open"
