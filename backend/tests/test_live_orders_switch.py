"""PUT /api/system/live-orders flips the cached setting + persists to .env.
   Run: python -m tests.test_live_orders_switch
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


def _run() -> None:
    async def _t():
        from app.auth.gate import require_auth
        from app.config import get_settings
        from app.routers import system as sys_mod

        # Redirect .env writes to a temp file (never touch the real backend/.env).
        tmp = Path(tempfile.mkdtemp()) / ".env"
        tmp.write_text("EXISTING=1\n")
        sys_mod._ENV_PATH = tmp

        s = get_settings()
        assert s.enable_live_orders is False, "test expects the default (gate OFF)"

        # ON: returns new state, flips the cached singleton, persists to .env.
        res = await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=True))
        assert res == {"live_orders_enabled": True}, res
        assert get_settings().enable_live_orders is True
        text = tmp.read_text()
        assert "ENABLE_LIVE_ORDERS=true" in text and "EXISTING=1" in text, text

        # OFF: plain false body, instant.
        res = await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=False))
        assert res == {"live_orders_enabled": False}, res
        assert get_settings().enable_live_orders is False
        assert "ENABLE_LIVE_ORDERS=false" in tmp.read_text()

        # A failed .env write must NOT leave the in-memory gate flipped.
        sys_mod._ENV_PATH = Path("/nonexistent-dir-for-test/.env")
        try:
            await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=True))
            raised = False
        except OSError:
            raised = True
        assert raised, "expected OSError from unwritable .env path"
        assert get_settings().enable_live_orders is False, \
            "gate flipped despite persist failure"

        # Route must be auth-gated like /resume.
        route = next(r for r in sys_mod.router.routes
                     if r.path == "/api/system/live-orders")
        assert require_auth in [d.call for d in route.dependant.dependencies], \
            "PUT /api/system/live-orders is missing require_auth"
        print("PASS: live-orders switch endpoint")
    asyncio.run(_t())
    print("\nALL LIVE-ORDERS-SWITCH TESTS PASSED")


if __name__ == "__main__":
    _run()


# ---------------------------------------------------------------- pytest: I5a
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import update  # noqa: E402


async def _close_all_open_trades():
    from app.db.base import init_db, session_scope
    from app.db.models import TradeRecord
    await init_db()
    async with session_scope() as s:
        await s.execute(update(TradeRecord).where(TradeRecord.status == "open")
                        .values(status="closed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [False, True])
async def test_flip_refused_while_a_trade_is_open(monkeypatch, tmp_path, start):
    """Flipping the gate mid-position would route real orders for a trade Alpaca never
    saw (simulated -> ON) or strand a transmitted one (ON -> OFF): 409, nothing changes."""
    from app.config import get_settings
    from app.db.base import session_scope
    from app.db.models import TradeRecord
    from app.routers import system as sys_mod

    await _close_all_open_trades()
    env = tmp_path / ".env"
    env.write_text(f"ENABLE_LIVE_ORDERS={'true' if start else 'false'}\n")
    monkeypatch.setattr(sys_mod, "_ENV_PATH", env)
    monkeypatch.setattr(get_settings(), "enable_live_orders", start)
    async with session_scope() as s:
        row = TradeRecord(strategy_key="strategy_1", symbol="AAPL", side="buy", quantity=1,
                          entry_price=100.0, status="open", transmitted=start)
        s.add(row)
        await s.flush()
        rid = row.id
    try:
        with pytest.raises(HTTPException) as ei:
            await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=not start))
        assert ei.value.status_code == 409
        assert "open trade" in ei.value.detail
        assert get_settings().enable_live_orders is start, "gate must not flip"
        assert env.read_text() == f"ENABLE_LIVE_ORDERS={'true' if start else 'false'}\n"
        # re-asserting the current value is not a flip
        res = await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=start))
        assert res == {"live_orders_enabled": start}
    finally:
        async with session_scope() as s:
            await s.delete(await s.get(TradeRecord, rid))


@pytest.mark.asyncio
async def test_flip_allowed_when_no_trade_is_open(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.routers import system as sys_mod

    await _close_all_open_trades()
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n")
    monkeypatch.setattr(sys_mod, "_ENV_PATH", env)
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    assert await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=True)) == \
        {"live_orders_enabled": True}
    assert "ENABLE_LIVE_ORDERS=true" in env.read_text()
    assert await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=False)) == \
        {"live_orders_enabled": False}
