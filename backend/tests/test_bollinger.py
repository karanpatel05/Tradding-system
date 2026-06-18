"""Tests for Bollinger intraday mean reversion (rolling SMA/std, long & short, exit at mean)."""

from __future__ import annotations


def _bars(closes, date="2025-01-02"):
    return [{"open": c, "high": c, "low": c, "close": c, "date": date} for c in closes]


def test_rolling_sma_std_population():
    from app.strategies.mean_reversion.bollinger import rolling_sma_std
    sma, std = rolling_sma_std([2, 4, 6], 3)
    assert sma[2] == 4.0
    # population std of [2,4,6] = sqrt((4+0+4)/3) = sqrt(8/3)
    assert abs(std[2] - (8 / 3) ** 0.5) < 1e-9
    assert sma[0] is None and std[1] is None


def test_long_when_below_band_exit_at_mean():
    from app.strategies.mean_reversion.bollinger import simulate_bollinger
    # Flat-ish series (tight band), one sharp dip below -1.5σ, then recovery back to mean.
    closes = [100, 100, 100, 100, 95, 100, 100]
    trades = simulate_bollinger(_bars(closes), window=4, num_std=1.5, slip_bps=0.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["entry_price"] == 95          # entered at the dip close
    assert t["exit_reason"] == "mean"
    assert t["ret"] > 0                     # bought low, reverted up


def test_short_when_above_band():
    from app.strategies.mean_reversion.bollinger import simulate_bollinger
    closes = [100, 100, 100, 100, 105, 100, 100]
    trades = simulate_bollinger(_bars(closes), window=4, num_std=1.5, slip_bps=0.0)
    assert len(trades) == 1
    assert trades[0]["side"] == "short"
    assert trades[0]["ret"] > 0             # shorted high, reverted down


def test_breakout_mode_flips_entry_side():
    from app.strategies.mean_reversion.bollinger import simulate_bollinger
    # Up-spike above +σ: reversion shorts it, breakout (opposite) goes long.
    closes = [100, 100, 100, 100, 105, 100, 100]
    rev = simulate_bollinger(_bars(closes), window=4, num_std=1.5, slip_bps=0.0, mode="reversion")
    brk = simulate_bollinger(_bars(closes), window=4, num_std=1.5, slip_bps=0.0, mode="breakout")
    assert rev[0]["side"] == "short"
    assert brk[0]["side"] == "long"


def test_trend_filter_blocks_counter_trend_fade():
    from app.strategies.mean_reversion.bollinger import simulate_bollinger
    # Dip below the band while price sits below the trend SMA (a "downtrend" per filter):
    # the long fade is counter-trend and should be blocked.
    closes = [100, 100, 100, 100, 100, 95, 100]
    no_filter = simulate_bollinger(_bars(closes), window=4, num_std=1.5, slip_bps=0.0)
    filtered = simulate_bollinger(_bars(closes), window=4, num_std=1.5, slip_bps=0.0,
                                  trend_period=6)
    assert len(no_filter) == 1 and no_filter[0]["side"] == "long"
    assert filtered == []   # counter-trend long filtered out


def test_eod_flat_closes_position():
    from app.strategies.mean_reversion.bollinger import simulate_bollinger
    # Dip below band on day 1 that never reverts to mean within the day → eod_flat closes it.
    bars = _bars([100, 100, 100, 100, 90, 88], date="2025-01-02")
    bars += _bars([88], date="2025-01-03")
    trades = simulate_bollinger(bars, window=4, num_std=1.5, slip_bps=0.0, eod_flat=True)
    assert trades and trades[0]["exit_reason"] == "eod_flat"
    assert trades[0]["exit_date"] == "2025-01-02"   # closed at end of entry day
