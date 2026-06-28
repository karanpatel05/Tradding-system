"""Live parity for the RTH gate: live_decision must block an entry that would
otherwise fire when the (latest) bar is outside RTH, and allow it inside RTH —
using the same session_flags the backtest uses.

Run: python -m tests.test_crossover_live_rth
"""

from __future__ import annotations

import asyncio
import glob
import json
from dataclasses import replace
from datetime import datetime, timezone


async def _run() -> None:
    from app.strategies.ema_crossover.params import EmaCrossoverParams
    from app.strategies.ema_crossover.signals import Series, State, decide_index
    from app.strategies.ema_crossover.strategy import EmaCrossoverStrategy

    files = sorted(glob.glob("data_cache/ibkr__*__5m.json"))
    if not files:
        print("SKIP test_crossover_live_rth: no cached 5m bars "
              "(cache purged - repopulate from IBKR to run this test)")
        return
    bars = json.load(open(files[0])).get("bars", [])
    sym = files[0].split("__")[1]

    p = replace(EmaCrossoverParams.from_settings(), use_htf_filter=False)

    # Find a real bar index where a flat entry fires (gate-agnostic decide).
    series = Series(bars, p)
    entry_i = None
    for i in range(1, len(bars)):
        a = decide_index(bars, series, i, State(), p, allow_long=True, allow_short=True,
                         can_open=True, must_flat=False)
        if a.kind == "enter":
            entry_i = i
            break
    assert entry_i is not None, "no entry signal found in sample bars"

    strat = EmaCrossoverStrategy()
    strat.params = p

    async def _no_htf(_sym):
        return []

    # Case 1: last bar IN RTH (15:00 UTC = 11:00 ET) → entry allowed.
    bars_in = [dict(b) for b in bars[: entry_i + 1]]
    bars_in[-1]["time"] = int(datetime(2025, 6, 5, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    strat.fetch_intraday = lambda _s, _b=bars_in: _ret(_b)
    strat._fetch_htf = _no_htf
    action_in, _ = await strat.live_decision(sym, State(), direction="both")
    assert action_in is not None and action_in.kind == "enter", \
        f"expected enter in RTH, got {action_in.kind if action_in else None}"
    print("PASS: live entry fires when latest bar is in RTH")

    # Case 2: SAME entry bar shifted to pre-market (12:00 UTC = 08:00 ET) → blocked.
    bars_out = [dict(b) for b in bars[: entry_i + 1]]
    bars_out[-1]["time"] = int(datetime(2025, 6, 5, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    strat.fetch_intraday = lambda _s, _b=bars_out: _ret(_b)
    action_out, _ = await strat.live_decision(sym, State(), direction="both")
    assert action_out is None or action_out.kind != "enter", \
        f"expected entry blocked out of RTH, got {action_out.kind if action_out else None}"
    print("PASS: live entry blocked when latest bar is outside RTH")

    print("\nALL CROSSOVER LIVE-RTH TESTS PASSED")


async def _ret(b):
    return b


if __name__ == "__main__":
    asyncio.run(_run())
