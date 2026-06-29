"""Gap gate: stale only when market open and the latest bar is missing.
   Run: python -m tests.test_gap_gate
"""
from __future__ import annotations


def _run() -> None:
    from app.services.backfill import interval_seconds, is_stale

    assert interval_seconds("5m") == 300 and interval_seconds("1d") == 86400
    assert interval_seconds("weird") == 300
    now = 1_700_000_000_000
    fresh = [{"time": now - 60_000}]          # 1 min old, 5m interval
    old = [{"time": now - 600_000}]           # 10 min old > 1.5*300s

    assert is_stale([], 300, now, market_open=True) is True       # no bars
    assert is_stale(old, 300, now, market_open=True) is True      # gap
    assert is_stale(fresh, 300, now, market_open=True) is False   # fresh
    assert is_stale(old, 300, now, market_open=False) is False    # closed → never stale
    print("PASS: gap gate evaluates staleness")
    print("\nALL GAP-GATE TESTS PASSED")


if __name__ == "__main__":
    _run()
