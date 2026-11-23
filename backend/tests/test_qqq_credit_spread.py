"""The QQQ first-hour credit spread runs in-app on Alpaca PAPER, and only when it is safe:

* refused while the external daemon's own Alpaca paper copy (--alpaca-paper) is on, or
  when its command line cannot be read (same paper account; fail closed);
* refused while order routing (ENABLE_LIVE_ORDERS) is off - it never runs on simulated
  fills - or without Alpaca keys or a usable checkpoint model;
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
from app.strategies.qqq_credit_spread import logic as L
from app.strategies.qqq_credit_spread.params import QqqSpreadParams

KEY = "qqq_credit_spread"


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "models" / "QQQ"
    d.mkdir(parents=True)
    (d / "model.joblib").write_bytes(b"x")
    (d / "model_metadata.json").write_text(json.dumps({
        "ticker": "QQQ", "features": L.FEATURES, "threshold": 0.9,
        "trained_at_utc": datetime.now().astimezone().isoformat()}))
    return tmp_path / "models"


@pytest.fixture
def routing_on(monkeypatch):
    from app.broker import alpaca_client as ac
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    monkeypatch.setattr(ac, "configured", lambda: True)


def _strategy(tmp_path, model_dir, lock: str | None = None, cmd: str | None = None,
              monkeypatch=None):
    live = tmp_path / "live"
    (live / "state").mkdir(parents=True, exist_ok=True)
    if lock is not None:
        (live / "state" / ".live_strategy.lock").write_text(lock)
    s = QqqCreditSpreadStrategy(live_dir=live, params=QqqSpreadParams(model_dir=str(model_dir)))
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


def test_ready_when_daemon_paper_copy_is_off(tmp_path, model_dir, routing_on, monkeypatch):
    s = _strategy(tmp_path, model_dir, lock=str(os.getpid()),
                  cmd="python live_strategy.py --loop --live --only QQQ", monkeypatch=monkeypatch)
    assert s.activation_block_reason() is None
    assert s.daemon_status()["alpaca_paper_copy"] is False


def test_refused_while_daemon_paper_copy_is_on(tmp_path, model_dir, routing_on, monkeypatch):
    s = _strategy(tmp_path, model_dir, lock=str(os.getpid()),
                  cmd="python live_strategy.py --loop --live --alpaca-paper", monkeypatch=monkeypatch)
    assert "--alpaca-paper" in s.activation_block_reason()


def test_refused_when_daemon_command_unreadable(tmp_path, model_dir, routing_on, monkeypatch):
    s = _strategy(tmp_path, model_dir, lock=str(os.getpid()), cmd=None, monkeypatch=monkeypatch)
    assert "cannot be read" in s.activation_block_reason()


def test_unreadable_lock_fails_closed(tmp_path, model_dir, routing_on, monkeypatch):
    s = _strategy(tmp_path, model_dir, lock="garbage", cmd=None, monkeypatch=monkeypatch)
    assert s.activation_block_reason()


def test_daemon_not_running_is_fine(tmp_path, model_dir, routing_on):
    s = _strategy(tmp_path, model_dir)
    assert s.activation_block_reason() is None


def test_refused_while_order_routing_is_off(tmp_path, model_dir, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    s = _strategy(tmp_path, model_dir)
    assert "Order routing is off" in s.activation_block_reason()


def test_refused_without_a_usable_model(tmp_path, routing_on):
    s = _strategy(tmp_path, tmp_path / "no-models")
    assert "model unavailable" in s.activation_block_reason()


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
async def test_blocked_strategy_is_reset_and_never_resumed(monkeypatch):
    from app.config import get_settings
    from app.db.base import init_db, session_scope
    from app.db.models import StrategyState
    from app.engine.manager import engine_manager
    from app.main import _resume_active_workers

    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    await init_db()
    await registry.seed_strategy_state()
    async with session_scope() as s:
        (await s.get(StrategyState, KEY)).status = "scanning"
    await registry.seed_strategy_state()
    async with session_scope() as s:
        assert (await s.get(StrategyState, KEY)).status == "idle"
        (await s.get(StrategyState, KEY)).status = "active"
    await _resume_active_workers()
    assert not engine_manager.is_running(KEY)
