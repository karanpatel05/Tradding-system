"""Manual live check (needs IB Gateway connected + OPRA + market open). NOT a unit test.

    python -m scripts.triple_calendar_live_smoke           # paper: builds + logs the entry
    ENABLE_LIVE_ORDERS=1 python -m scripts.triple_calendar_live_smoke   # places + flattens
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from app.engine.execution import flatten_all
from app.ibkr.gateway import get_gateway
from app.strategies.triple_calendar.live import entry_legs
from app.strategies.triple_calendar.live_source import LivePriceSource
from app.strategies.triple_calendar.strategy import decide_entry
from app.strategies.triple_calendar.strategy_class import _params, _resolve_snapshot


async def main() -> int:
    await get_gateway().connect()
    params = _params()
    src = LivePriceSource()
    today = datetime.now().date()
    spot = await src.aunderlying(params.underlying)
    if spot is None:
        print(f"No {params.underlying} quote — check connection/market open.")
        get_gateway().disconnect()
        return 1
    strikes = await src.achain(params.underlying)
    print(f"{params.underlying} spot {spot} | {len(strikes)} strikes")
    snapshot = await _resolve_snapshot(src, params, spot, strikes, today, None)
    pos = decide_entry(today, snapshot, params, params.event_dates, None)
    if pos is None:
        print("No entry built (no option quotes / filters). Check OPRA entitlement + market open.")
    else:
        print(f"Entry: {len(entry_legs(pos))} legs across "
              f"{sorted({c.strike for c in pos.calendars})}, net debit {pos.net_debit:.2f}")
        input("Press Enter to flatten (only acts live)...")
        print("flatten:", await flatten_all())
    get_gateway().disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
