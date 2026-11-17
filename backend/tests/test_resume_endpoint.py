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
