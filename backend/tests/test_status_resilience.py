"""/api/auth/status payload exposes conn_state + safe_mode + reason.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_st.db python -m tests.test_status_resilience
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db
        from app.auth.routes import _status
        from app.engine.safe_mode import trip_safe_mode
        from app.ibkr.gateway import ConnState, get_gateway

        await init_db()
        get_gateway().set_state(ConnState.SAFE_MODE)
        await trip_safe_mode("orphan position AAPL")

        st = await _status()
        assert st.conn_state == "safe_mode", st.conn_state
        assert st.safe_mode is True and st.safe_mode_reason == "orphan position AAPL", st
        print("PASS: status exposes resilience fields")
    asyncio.run(_t())
    print("\nALL STATUS-RESILIENCE TESTS PASSED")


if __name__ == "__main__":
    _run()
