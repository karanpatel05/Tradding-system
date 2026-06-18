"""Tests for the volume-confirmed Donchian breakout (hourly aggregation, vol gate,
trailing stop). Deterministic."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _ibar(hhmm, o, h, l, c, v, date="2025-01-02"):
    y, m, d = (int(x) for x in date.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    t = datetime(y, m, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)
    return {"time": int(t.timestamp() * 1000), "open": o, "high": h, "low": l, "close": c, "volume": v}


def _hbar(o, h, l, c, v, date="2025-01-02"):
    return {"date": date, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_aggregate_hourly_bins_from_open():
    from app.strategies.breakout.volume_donchian import aggregate_hourly
    bars = [_ibar("09:30", 10, 10.5, 9.8, 10.2, 100),   # bin 0 (09:30-10:30)
            _ibar("09:45", 10.2, 10.7, 10.1, 10.6, 50),  # bin 0
            _ibar("10:30", 10.6, 11.0, 10.5, 10.9, 70),  # bin 1 (10:30-11:30)
            _ibar("18:00", 99, 99, 99, 99, 999)]          # after-hours dropped
    h = aggregate_hourly(bars)
    assert len(h) == 2
    assert h[0]["open"] == 10 and h[0]["close"] == 10.6
    assert h[0]["high"] == 10.7 and h[0]["low"] == 9.8 and h[0]["volume"] == 150
    assert h[1]["close"] == 10.9


def test_long_breakout_needs_volume():
    from app.strategies.breakout.volume_donchian import simulate
    base = [_hbar(10, 10.1, 9.9, 10.0, 100) for _ in range(5)]
    # Breakout above 20(=3)-high with volume 2x avg -> long; without volume -> nothing.
    brk_hi_vol = base + [_hbar(10, 11.0, 10.0, 11.0, 300),
                         _hbar(11, 11.5, 10.9, 11.4, 100),
                         _hbar(11, 11.0, 8.0, 8.2, 100)]   # deep drop -> trailing stop
    brk_no_vol = base + [_hbar(10, 11.0, 10.0, 11.0, 100),  # same break, weak volume
                         _hbar(11, 11.5, 10.9, 11.4, 100)]
    t1 = simulate(brk_hi_vol, channel=3, atr_period=2, atr_mult=2.0, slip_bps=0.0)
    t2 = simulate(brk_no_vol, channel=3, atr_period=2, atr_mult=2.0, slip_bps=0.0)
    assert len(t1) >= 1 and t1[0]["side"] == "long" and t1[0]["entry_price"] == 11.0
    assert t1[0]["exit_reason"] in ("trail_stop", "reverse", "eod")
    assert t2 == []   # no volume confirmation -> no trade


def test_short_breakdown_with_volume():
    from app.strategies.breakout.volume_donchian import simulate
    base = [_hbar(10, 10.1, 9.9, 10.0, 100) for _ in range(5)]
    bars = base + [_hbar(10, 10.0, 9.0, 9.0, 300),         # break below 3-low w/ volume
                   _hbar(9, 9.1, 8.5, 8.6, 100),
                   _hbar(8.6, 12.0, 8.5, 11.9, 100)]        # spike up -> trailing stop
    t = simulate(bars, channel=3, atr_period=2, atr_mult=2.0, slip_bps=0.0)
    assert len(t) >= 1 and t[0]["side"] == "short"
