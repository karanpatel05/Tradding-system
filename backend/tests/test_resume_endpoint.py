"""resume() clears safe mode + recovers to READY on clean state.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_re.db python -m tests.test_resume_endpoint
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.broker import status as st
        from app.db.base import init_db
        from app.engine import recovery as rec_mod
        from app.engine.safe_mode import is_safe_mode, trip_safe_mode
        from app.routers.system import resume

        await init_db()
        await trip_safe_mode("orphan AAPL")

        # Clean broker snapshot -> recovery clears (no discrepancies).
        async def clean_snap():
            return {"positions": [], "fills": [], "orders": [], "issues": []}
        async def no_local():
            return {}
        rec_mod.fetch_snapshot = clean_snap
        rec_mod.local_open_positions = no_local

        # Tastytrade OAuth probe succeeds (see test_broker_status.py's pattern).
        async def ok_probe(force):
            return "ok"
        st._do_probe = ok_probe
        st._cache.update(result=None, at=0.0)

        res = await resume()
        assert res["conn_state"] == "ready", res
        assert res["safe_mode"] is False and await is_safe_mode() is False
        print("PASS: resume cleared safe mode + recovered to READY")
    asyncio.run(_t())
    print("\nALL RESUME-ENDPOINT TESTS PASSED")


if __name__ == "__main__":
    _run()


# ---------------------------------------------------------------- pytest: I6
import pytest  # noqa: E402


async def _boom():
    raise RuntimeError("alpaca exploded")


async def _ok_probe(force=False):
    return "ok"


async def _reason():
    from app.db.base import session_scope
    from app.db.models import SystemFlag
    async with session_scope() as s:
        row = await s.get(SystemFlag, "safe_mode")
        return row.reason if row and row.on else None


@pytest.mark.asyncio
async def test_resume_trips_safe_mode_when_recovery_raises(monkeypatch):
    """/resume clears safe mode then re-runs recovery; if recovery raises, the system must
    not be left armed with safe mode off."""
    from app.db.base import init_db
    from app.engine.safe_mode import clear_safe_mode, is_safe_mode
    from app.routers import system as sys_mod

    await init_db()
    monkeypatch.setattr(sys_mod, "run_recovery", _boom)
    monkeypatch.setattr(sys_mod, "probe", _ok_probe)
    try:
        res = await sys_mod.resume()
        assert await is_safe_mode() is True
        assert res["safe_mode"] is True and res["conn_state"] == "safe_mode"
        assert (await _reason()).startswith("recovery failed on resume: RuntimeError")
    finally:
        await clear_safe_mode()


@pytest.mark.asyncio
async def test_startup_recovery_failure_trips_safe_mode(monkeypatch):
    from app import main
    from app.db.base import init_db
    from app.engine import recovery
    from app.engine.safe_mode import clear_safe_mode, is_safe_mode

    await init_db()
    await clear_safe_mode()
    monkeypatch.setattr(recovery, "run_recovery", _boom)
    try:
        await main._startup_recovery()          # must not raise: boot continues
        assert await is_safe_mode() is True
        assert (await _reason()).startswith("startup recovery failed: RuntimeError")
    finally:
        await clear_safe_mode()


@pytest.mark.asyncio
async def test_startup_recovery_survives_safe_mode_write_failure(monkeypatch):
    from app import main
    from app.engine import recovery, safe_mode

    async def cannot_trip(reason):
        raise OSError("db locked")
    monkeypatch.setattr(recovery, "run_recovery", _boom)
    monkeypatch.setattr(safe_mode, "trip_safe_mode", cannot_trip)
    await main._startup_recovery()              # still must not raise
