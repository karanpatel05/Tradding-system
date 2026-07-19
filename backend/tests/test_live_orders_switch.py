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
