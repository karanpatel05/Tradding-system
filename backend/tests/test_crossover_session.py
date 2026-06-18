"""RTH session gating for the EMA crossover.

Entries allowed only within [session_start_et, session_cutoff_et); any open
position is force-flat at/after session_close_et. ET times in June = EDT (UTC-4),
so 09:30 ET = 13:30 UTC, 15:55 ET = 19:55 UTC, 16:00 ET = 20:00 UTC.

Run: python -m tests.test_crossover_session
"""

from __future__ import annotations

from dataclasses import dataclass


def _ms(y, mo, d, h, mi):
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def _run() -> None:
    from app.strategies.ema_crossover.session import can_open, must_flat, session_flags

    START, CUTOFF, CLOSE = "09:30", "15:55", "16:00"

    # --- can_open: True only inside [09:30, 15:55) ET ---
    assert can_open(_ms(2025, 6, 5, 14, 0), START, CUTOFF) is True    # 10:00 ET
    assert can_open(_ms(2025, 6, 5, 13, 30), START, CUTOFF) is True   # 09:30 ET (inclusive start)
    assert can_open(_ms(2025, 6, 5, 19, 50), START, CUTOFF) is True   # 15:50 ET
    assert can_open(_ms(2025, 6, 5, 12, 0), START, CUTOFF) is False   # 08:00 ET pre-market
    assert can_open(_ms(2025, 6, 5, 19, 55), START, CUTOFF) is False  # 15:55 ET (exclusive cutoff)
    assert can_open(_ms(2025, 6, 5, 23, 0), START, CUTOFF) is False   # 19:00 ET after-hours
    print("PASS: can_open gates entries to [09:30, 15:55) ET")

    # --- must_flat: True at/after 16:00 ET ---
    assert must_flat(_ms(2025, 6, 5, 20, 0), CLOSE) is True    # 16:00 ET (inclusive)
    assert must_flat(_ms(2025, 6, 5, 20, 30), CLOSE) is True   # 16:30 ET
    assert must_flat(_ms(2025, 6, 5, 19, 58), CLOSE) is False  # 15:58 ET
    print("PASS: must_flat fires at/after 16:00 ET")

    # --- session_flags(params): respects use_rth_filter, handles None time ---
    @dataclass
    class P:
        use_rth_filter: bool = True
        session_start_et: str = START
        session_cutoff_et: str = CUTOFF
        session_close_et: str = CLOSE

    assert session_flags(_ms(2025, 6, 5, 14, 0), P()) == (True, False)    # RTH
    assert session_flags(_ms(2025, 6, 5, 12, 0), P()) == (False, False)   # pre-market: no entry, not flat
    assert session_flags(_ms(2025, 6, 5, 20, 30), P()) == (False, True)   # after close: no entry, flat
    # filter off → never gates
    assert session_flags(_ms(2025, 6, 5, 12, 0), P(use_rth_filter=False)) == (True, False)
    # missing timestamp → safe default (no entry-block, no forced flat)
    assert session_flags(None, P()) == (True, False)
    print("PASS: session_flags respects use_rth_filter and None time")

    print("\nALL CROSSOVER SESSION TESTS PASSED")


if __name__ == "__main__":
    _run()
