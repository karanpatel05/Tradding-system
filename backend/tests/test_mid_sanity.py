"""Each sanity rule accepts/rejects correctly; the jump memory stores ACCEPTED mids only,
so one bad quote can't poison the baseline. Uses explicit `now` for determinism."""
import pytest
from app.marketdata.sanity import MidSanity


def _s():
    return MidSanity(rel_spread=0.35, abs_spread=2.50, max_jump_pct=0.50, window_seconds=900)


def test_missing_or_crossed_rejected():
    s = _s()
    assert s.check("k", None, 5.0) == (None, "missing")
    assert s.check("k", 5.0, None) == (None, "missing")
    assert s.check("k", 0.0, 5.0) == (None, "missing")
    assert s.check("k", 5.2, 5.0) == (None, "crossed")


def test_wide_spread_rejected_narrow_accepted():
    s = _s()
    # mid 4.0, spread 1.0 <= max(2.50, 1.40) -> accepted
    assert s.check("k", 3.5, 4.5, now=0.0) == (4.0, None)
    # mid 4.0, spread 3.0 > max(2.50, 0.35*4.0=1.40) -> rejected
    assert s.check("k2", 2.5, 5.5, now=0.0) == (None, "wide_spread")
    # big mid: spread 3.0 <= 0.35*20=7.0 -> accepted
    assert s.check("k3", 18.5, 21.5, now=0.0) == (20.0, None)


def test_jump_rejected_within_window_and_memory_not_poisoned():
    s = _s()
    assert s.check("k", 3.9, 4.1, now=0.0) == (4.0, None)      # baseline 4.0
    mid, reason = s.check("k", 57.0, 59.0, now=60.0)           # 58 vs 4 -> jump
    assert (mid, reason) == (None, "jump")
    # baseline is still 4.0 (rejected mid NOT stored): 4.2 accepted
    mid, reason = s.check("k", 4.1, 4.3, now=120.0)
    assert mid == pytest.approx(4.2) and reason is None
    # outside the window the jump rule doesn't apply
    assert s.check("k", 7.9, 8.1, now=120.0 + 901.0) == (8.0, None)
