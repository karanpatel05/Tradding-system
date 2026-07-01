"""Close orphan/open Triple Calendar position records so the strategy resets to a
consistent flat state (only one open record is valid at a time). Marks each open
OptionPositionRecord as closed with an exit_time; it does NOT alter P&L values.

DRY-RUN by default — shows what it would close. Pass --yes to actually close.

    cd backend
    DATABASE_URL="sqlite+aiosqlite:///./trading.db" ../.venv/bin/python -m scripts.close_orphan_tc        # preview
    DATABASE_URL="sqlite+aiosqlite:///./trading.db" ../.venv/bin/python -m scripts.close_orphan_tc --yes  # apply

After applying, restart the server so the worker reloads a flat state
(e.g. `lsof -ti tcp:8765 | xargs kill` — the LaunchAgent restarts it).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone


async def main() -> int:
    from app.db.base import session_scope
    from app.db.models import OptionPositionRecord
    from sqlalchemy import select

    apply = "--yes" in sys.argv
    async with session_scope() as s:
        rows = (await s.execute(
            select(OptionPositionRecord).where(OptionPositionRecord.status == "open"))).scalars().all()
        if not rows:
            print("No open Triple Calendar records — nothing to close.")
            return 0
        print(f"Open records ({len(rows)}):")
        for r in rows:
            print(f"  #{r.id} {r.strategy_key} {r.underlying} debit={r.net_debit} "
                  f"pnl={r.pnl} entry={r.entry_time}")
        if not apply:
            print("\nDRY RUN — pass --yes to close these (status=closed, exit_time=now; P&L unchanged).")
            return 0
        for r in rows:
            r.status = "closed"
            r.exit_time = datetime.now(timezone.utc)
        print(f"\nClosed {len(rows)} record(s). Strategy is now flat. Restart the server to reload state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
