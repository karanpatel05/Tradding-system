"""The QQQ first-hour credit spread is registered but switched off in this app.

It runs outside the app (the com.karan.spreadlive LaunchAgent, real Tastytrade orders),
so the app must never start a copy: activation is refused, it is never auto-resumed at
boot, and a persisted "scanning" status is reset on seed.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_qqq.db ../.venv/bin/python -m pytest tests/test_qqq_credit_spread.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.strategies import registry
from app.strategies.qqq_credit_spread import QqqCreditSpreadStrategy

KEY = "qqq_credit_spread"


def _live_dir(tmp_path, lock: str | None = None, state: dict | None = None):
    (tmp_path / "state").mkdir()
    if lock is not None:
        (tmp_path / "state" / ".live_strategy.lock").write_text(lock)
    if state is not None:
        (tmp_path / "state" / "live_state.json").write_text(json.dumps(state))
    return tmp_path


def _dead_pid() -> int:
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid -= 1


def test_registered_ready_with_real_rules():
    s = registry.get_strategy(KEY)
    assert isinstance(s, QqqCreditSpreadStrategy)
    assert s.ready is True
    assert s.external_runner is True
    ov = s.overview()
    assert ov["ready"] is True and ov["rules"] and ov["entry_logic"] and ov["exit_logic"]


def test_blocked_while_daemon_holds_the_lock(tmp_path):
    s = QqqCreditSpreadStrategy(live_dir=_live_dir(tmp_path, lock=str(os.getpid())))
    reason = s.activation_block_reason()
    assert reason and "already running" in reason.lower()
    assert s.external_status()["daemon_running"] is True


def test_blocked_even_when_daemon_is_down(tmp_path):
    s = QqqCreditSpreadStrategy(live_dir=_live_dir(tmp_path, lock=str(_dead_pid())))
    assert s.external_status()["daemon_running"] is False
    reason = s.activation_block_reason()
    assert reason and "not built" in reason.lower()


def test_no_lock_file_is_not_running_but_still_blocked(tmp_path):
    s = QqqCreditSpreadStrategy(live_dir=_live_dir(tmp_path))
    assert s.external_status()["daemon_running"] is False
    assert s.activation_block_reason()


def test_unreadable_lock_fails_closed(tmp_path):
    s = QqqCreditSpreadStrategy(live_dir=_live_dir(tmp_path, lock="garbage"))
    assert s.external_status()["daemon_running"] is True


def test_missing_live_dir_degrades(tmp_path):
    s = QqqCreditSpreadStrategy(live_dir=tmp_path / "nope")
    st = s.external_status()
    assert st["found"] is False and st["daemon_running"] is False
    assert s.activation_block_reason()


def test_today_summary_from_state_file(tmp_path):
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    state = {today: {
        "entry": "done", "checkpoint": "done", "final_close": "pending",
        "entries": {"QQQ": {"status": "filled", "right": "C", "short_strike": 660.0,
                            "long_strike": 661.0, "quantity": 1,
                            "filled_credit_per_share": 0.49}},
    }}
    s = QqqCreditSpreadStrategy(live_dir=_live_dir(tmp_path, state=state))
    t = s.external_status()["today"]
    assert t["session_date"] == today
    assert t["entry"] == "done" and t["final_close"] == "pending"
    assert t["position"] == {"ticker": "QQQ", "spread": "call credit spread",
                             "short_strike": 660.0, "long_strike": 661.0,
                             "quantity": 1, "credit_per_share": 0.49,
                             "status": "filled"}


@pytest.mark.asyncio
async def test_activate_endpoint_refuses_with_409():
    from app.db.base import init_db
    from app.engine.manager import engine_manager
    from app.routers.strategies import activate

    await init_db()
    await registry.seed_strategy_state()
    with pytest.raises(HTTPException) as exc:
        await activate(KEY)
    assert exc.value.status_code == 409
    assert not engine_manager.is_running(KEY)


@pytest.mark.asyncio
async def test_seed_resets_persisted_scanning_and_boot_does_not_resume():
    from app.db.base import init_db, session_scope
    from app.db.models import StrategyState
    from app.engine.manager import engine_manager
    from app.main import _resume_active_workers

    await init_db()
    await registry.seed_strategy_state()
    async with session_scope() as s:
        (await s.get(StrategyState, KEY)).status = "scanning"
    await registry.seed_strategy_state()
    async with session_scope() as s:
        assert (await s.get(StrategyState, KEY)).status == "idle"
        (await s.get(StrategyState, KEY)).status = "active"   # bypass seed entirely
    await _resume_active_workers()
    assert not engine_manager.is_running(KEY)


@pytest.mark.asyncio
async def test_allocation_and_direction_refused():
    from app.db.base import init_db
    from app.routers.strategies import (AllocationBody, DirectionBody, set_allocation,
                                        set_direction)

    await init_db()
    await registry.seed_strategy_state()
    with pytest.raises(HTTPException) as exc:
        await set_allocation(KEY, AllocationBody(amount=1000))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await set_direction(KEY, DirectionBody(direction="long"))
    assert exc.value.status_code == 409
