"""send_alert dispatches to channels + default writes a journal event; bad channel skipped.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_al.db python -m tests.test_alerts
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.db.base import init_db, session_scope
        from app.db.models import LogEntry
        from app.services import alerts
        from sqlalchemy import select

        await init_db()
        got = []
        async def good(kind, msg, data): got.append((kind, msg))
        async def bad(kind, msg, data): raise RuntimeError("boom")
        alerts.register_channel(good)
        alerts.register_channel(bad)

        await alerts.send_alert("safe_mode", "halt: orphan AAPL", {"x": 1})
        assert ("safe_mode", "halt: orphan AAPL") in got, got     # custom channel ran
        async with session_scope() as s:                          # default channel wrote a journal event
            rows = (await s.execute(select(LogEntry).where(LogEntry.category == "system"))).scalars().all()
        assert any("orphan AAPL" in r.message for r in rows), [r.message for r in rows]
        print("PASS: send_alert dispatched + journaled, bad channel skipped")
    asyncio.run(_t())
    print("\nALL ALERTS TESTS PASSED")


if __name__ == "__main__":
    _run()
