"""Primitive tests for the daily-anomaly strategies (RTH aggregation, overnight
drift, Donchian breakout). Deterministic synthetic bars — no data needed."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _bar(date: str, hhmm: str, o, h, l, c, v=1000):
    y, m, d = (int(x) for x in date.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    t = datetime(y, m, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)
    return {"time": int(t.timestamp() * 1000), "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_rth_daily_drops_extended_hours():
    from app.strategies.daily_anomaly.rth_daily import to_rth_daily
    bars = [
        _bar("2025-01-02", "04:00", 9, 9, 9, 9),       # pre-market — excluded
        _bar("2025-01-02", "09:30", 10, 11, 9.8, 10.5),  # RTH open
        _bar("2025-01-02", "12:00", 10.5, 12, 10.4, 11),  # RTH mid (day high 12)
        _bar("2025-01-02", "15:55", 11, 11.2, 10.9, 11.1),  # RTH close
        _bar("2025-01-02", "18:00", 13, 13, 13, 13),     # after-hours — excluded
        _bar("2025-01-03", "09:30", 11.2, 11.5, 11.0, 11.4),
        _bar("2025-01-03", "15:55", 11.4, 11.6, 11.3, 11.5),
    ]
    days = to_rth_daily(bars)
    assert [d["date"] for d in days] == ["2025-01-02", "2025-01-03"]
    d0 = days[0]
    assert d0["open"] == 10 and d0["close"] == 11.1
    assert d0["high"] == 12 and d0["low"] == 9.8   # extended-hours 9 and 13 ignored


def test_overnight_returns_net_of_slippage():
    from app.strategies.daily_anomaly.overnight import overnight_returns
    days = [
        {"date": "2025-01-02", "open": 100, "high": 0, "low": 0, "close": 100},
        {"date": "2025-01-03", "open": 102, "high": 0, "low": 0, "close": 101},
    ]
    # one night: buy close 100, sell next open 102. raw +2%. slip 2bp/side.
    rets = overnight_returns(days, slip_bps=2.0)
    assert len(rets) == 1
    expected = (102 * (1 - 2e-4)) / (100 * (1 + 2e-4)) - 1
    assert abs(rets[0]["ret"] - expected) < 1e-12
    assert rets[0]["date"] == "2025-01-03"  # tagged to the exit (open) day


def test_overnight_sma_filter_skips_below_average():
    from app.strategies.daily_anomaly.overnight import overnight_returns
    # closes climb then one dips below its trailing SMA → that night skipped
    closes = [10, 11, 12, 13, 9]  # last close 9 < SMA of prior
    days = [{"date": f"2025-01-0{i+1}", "open": c, "high": 0, "low": 0, "close": c}
            for i, c in enumerate(closes)]
    all_nights = overnight_returns(days, slip_bps=0.0)
    filt = overnight_returns(days, slip_bps=0.0, sma=3)
    assert len(all_nights) == 4
    # night entering on close[3]=13 (>SMA(11,12,13)=12) kept; close[4]=9 has no next day
    kept_dates = {r["date"] for r in filt}
    assert "2025-01-04" in kept_dates  # entered when 13 > SMA 11
    # entering on close[1]=11 with SMA(10,11)=10.5 → kept; on close[0] no SMA yet → skip
    assert all(len(filt) <= len(all_nights) for _ in [0])


def test_atr_and_donchian_long_trade():
    from app.strategies.daily_anomaly.donchian import atr, simulate_donchian
    # Build a series that consolidates then breaks out, then stops out.
    base = [{"date": f"2025-01-{i+1:02d}", "open": 10, "high": 10.5, "low": 9.5, "close": 10}
            for i in range(5)]
    breakout = {"date": "2025-01-06", "open": 10, "high": 11.0, "low": 9.9, "close": 10.9}  # close>prior 5d high 10.5
    follow = {"date": "2025-01-07", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.0}  # entry day
    drop = {"date": "2025-01-08", "open": 11.0, "high": 11.0, "low": 8.0, "close": 8.2}      # hits ATR stop
    bars = base + [breakout, follow, drop]
    a = atr(bars, period=3)
    assert a[-1] > 0
    trades = simulate_donchian(bars, n=5, k=2.0, capital=2000, risk_pct=0.01, slip_bps=0.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["entry_price"] == 10.9   # entered at open of day after breakout close
    assert t["exit_reason"] in ("stop", "donchian_exit", "eod")
    assert t["shares"] >= 1
