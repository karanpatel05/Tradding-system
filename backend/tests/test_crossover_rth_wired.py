"""RTH gate wired into the portfolio sim: with the filter on, NO trade may enter
outside [09:30, 15:55) ET, and any position is force-flat by 16:00 (reason 'eod').
With the filter off, some trades DO enter outside RTH (proving the gate changed
behavior, not that there were none to begin with).

Uses real cached 5m bars. Run: python -m tests.test_crossover_rth_wired
"""

from __future__ import annotations

import glob
import json
from dataclasses import replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _entry_et(ms) -> time:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_ET).time()


def _run() -> None:
    from app.engine.risk import RiskLimits
    from app.strategies.ema_crossover.params import EmaCrossoverParams
    from app.backtest.portfolio import simulate_portfolio

    files = sorted(glob.glob("data_cache/massive__*__5m.json"))
    assert files, "no cached 5m bars to test against (data_cache/massive__*__5m.json)"
    bars_by_symbol = {}
    for f in files[:4]:
        sym = f.split("__")[1]
        bars_by_symbol[sym] = json.load(open(f)).get("bars", [])

    base = EmaCrossoverParams.from_settings()
    base = replace(base, use_htf_filter=False)  # isolate the session gate
    limits = RiskLimits.from_settings()

    START, CUTOFF = time(9, 30), time(15, 55)
    CLOSE = time(16, 0)

    # --- filter ON: every entry inside [09:30, 15:55); nothing held past 16:00 ---
    on = replace(base, use_rth_filter=True)
    trades_on = simulate_portfolio(bars_by_symbol, on, limits, 2000.0, direction="both")
    assert trades_on, "expected some trades with the gate on"
    bad_entry = [t for t in trades_on if not (START <= _entry_et(t.trade.entry_time) < CUTOFF)]
    assert not bad_entry, f"{len(bad_entry)} trades entered outside RTH with gate ON"
    # any trade whose exit is past 16:00 ET must be an 'eod' force-flat
    late_exit = [t for t in trades_on
                 if _entry_et(t.trade.exit_time) >= CLOSE and t.trade.reason not in ("eod", "end")]
    assert not late_exit, f"{len(late_exit)} positions held past 16:00 without eod exit"
    print(f"PASS: gate ON — all {len(trades_on)} entries in RTH, none held past close")

    # --- filter OFF: some entries land outside RTH (gate is what removes them) ---
    off = replace(base, use_rth_filter=False)
    trades_off = simulate_portfolio(bars_by_symbol, off, limits, 2000.0, direction="both")
    outside = [t for t in trades_off if not (START <= _entry_et(t.trade.entry_time) < CUTOFF)]
    assert outside, "expected some out-of-RTH entries with the gate OFF (else test proves nothing)"
    print(f"PASS: gate OFF — {len(outside)}/{len(trades_off)} entries land outside RTH")

    print("\nALL CROSSOVER RTH-WIRING TESTS PASSED")


if __name__ == "__main__":
    _run()
