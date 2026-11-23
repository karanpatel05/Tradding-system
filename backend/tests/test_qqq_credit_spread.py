"""The QQQ first-hour credit spread runs in-app on Alpaca PAPER, and only when it is safe:

* refused while the external daemon's own Alpaca paper copy (--alpaca-paper) is on, or
  when its command line cannot be read (same paper account; fail closed);
* refused while order routing (ENABLE_LIVE_ORDERS) is off - it never runs on simulated
  fills - or without Alpaca keys;
* a blocked strategy is never auto-resumed at boot, and its persisted active status
  resets on seed.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_qqq.db ../.venv/bin/python -m pytest tests/test_qqq_credit_spread.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.strategies import registry
from app.strategies.qqq_credit_spread import QqqCreditSpreadStrategy
from app.strategies.qqq_credit_spread.params import QqqSpreadParams

KEY = "qqq_credit_spread"


@pytest.fixture
def routing_on(monkeypatch):
    from app.broker import alpaca_client as ac
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    monkeypatch.setattr(ac, "configured", lambda: True)


def _strategy(tmp_path, lock: str | None = None, cmd: str | None = None,
              monkeypatch=None):
    live = tmp_path / "live"
    (live / "state").mkdir(parents=True, exist_ok=True)
    if lock is not None:
        (live / "state" / ".live_strategy.lock").write_text(lock)
    s = QqqCreditSpreadStrategy(live_dir=live, params=QqqSpreadParams())
    if monkeypatch is not None:
        monkeypatch.setattr(QqqCreditSpreadStrategy, "_process_command",
                            staticmethod(lambda pid: cmd))
    return s


def test_registered_in_app_with_real_rules():
    s = registry.get_strategy(KEY)
    assert isinstance(s, QqqCreditSpreadStrategy)
    assert s.ready and not s.external_runner and hasattr(s, "run_live")
    ov = s.overview()
    assert ov["rules"] and "Alpaca PAPER" in ov["description"]
    assert "model" not in ov["entry_logic"].lower() and "15:50" in ov["exit_logic"]


def test_ready_when_daemon_paper_copy_is_off(tmp_path, routing_on, monkeypatch):
    s = _strategy(tmp_path, lock=str(os.getpid()),
                  cmd="python live_strategy.py --loop --live --only QQQ", monkeypatch=monkeypatch)
    assert s.activation_block_reason() is None
    assert s.daemon_status()["alpaca_paper_copy"] is False


def test_refused_while_daemon_paper_copy_is_on(tmp_path, routing_on, monkeypatch):
    s = _strategy(tmp_path, lock=str(os.getpid()),
                  cmd="python live_strategy.py --loop --live --alpaca-paper", monkeypatch=monkeypatch)
    assert "--alpaca-paper" in s.activation_block_reason()


def test_refused_when_daemon_command_unreadable(tmp_path, routing_on, monkeypatch):
    s = _strategy(tmp_path, lock=str(os.getpid()), cmd=None, monkeypatch=monkeypatch)
    assert "cannot be read" in s.activation_block_reason()


def test_unreadable_lock_fails_closed(tmp_path, routing_on, monkeypatch):
    s = _strategy(tmp_path, lock="garbage", cmd=None, monkeypatch=monkeypatch)
    assert s.activation_block_reason()


def test_daemon_not_running_is_fine(tmp_path, routing_on):
    s = _strategy(tmp_path)
    assert s.activation_block_reason() is None


def test_refused_while_order_routing_is_off(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    s = _strategy(tmp_path)
    assert "Order routing is off" in s.activation_block_reason()


@pytest.mark.asyncio
async def test_activate_endpoint_refuses_with_409_when_blocked(monkeypatch):
    from app.config import get_settings
    from app.db.base import init_db
    from app.engine.manager import engine_manager
    from app.routers.strategies import activate

    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    await init_db()
    await registry.seed_strategy_state()
    with pytest.raises(HTTPException) as exc:
        await activate(KEY)
    assert exc.value.status_code == 409
    assert not engine_manager.is_running(KEY)


@pytest.mark.asyncio
async def test_worker_that_was_on_resumes_even_when_activation_is_blocked(monkeypatch):
    """A transient refusal at boot (routing not yet read, ps unreadable, model briefly
    missing) must never switch off a worker that may be holding a spread: it resumes,
    and its runner gates only NEW entries."""
    import asyncio

    from app.config import get_settings
    from app.db.base import init_db, session_scope
    from app.db.models import StrategyState
    from app.engine.manager import engine_manager
    from app.main import _resume_active_workers

    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    strat = registry.get_strategy(KEY)
    assert strat.activation_block_reason()           # blocked for a NEW activation
    started = asyncio.Event()

    async def fake_run_live():
        started.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(strat, "run_live", fake_run_live)
    await init_db()
    await registry.seed_strategy_state()
    async with session_scope() as s:
        (await s.get(StrategyState, KEY)).status = "scanning"
    await registry.seed_strategy_state()
    async with session_scope() as s:
        assert (await s.get(StrategyState, KEY)).status == "scanning"   # not reset
    await _resume_active_workers()
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert engine_manager.is_running(KEY)
    finally:
        await engine_manager.stop_worker(KEY)
        async with session_scope() as s:
            (await s.get(StrategyState, KEY)).status = "idle"


def test_resume_refused_only_for_invalid_settings(tmp_path):
    good = _strategy(tmp_path)
    assert good.resume_block_reason() is None
    bad = QqqCreditSpreadStrategy(live_dir=tmp_path, params=QqqSpreadParams(reprice_seconds=0))
    assert "Invalid" in bad.resume_block_reason()
