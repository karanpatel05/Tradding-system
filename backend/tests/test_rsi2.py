"""Primitive tests for Connors RSI-2 mean reversion (RSI, SMA, one long trade).
Deterministic — no data needed. Small periods so trades are reachable in a few bars."""

from __future__ import annotations


def _bars(closes):
    # date strings just need to sort; OHLC collapsed to close for these unit tests.
    return [{"date": f"2025-01-{i+1:02d}", "open": c, "high": c, "low": c, "close": c}
            for i, c in enumerate(closes)]


def test_rsi_extremes():
    from app.strategies.mean_reversion.rsi2 import rsi
    up = rsi([1, 2, 3, 4, 5], 2)
    down = rsi([5, 4, 3, 2, 1], 2)
    assert up[-1] == 100.0          # only gains -> RSI 100
    assert down[-1] == 0.0          # only losses -> RSI 0
    assert up[0] is None and up[1] is None  # undefined before `period` deltas


def test_sma():
    from app.strategies.mean_reversion.rsi2 import sma
    s = sma([1, 2, 3, 4, 5], 3)
    assert s[4] == 4.0 and s[3] == 3.0     # mean(3,4,5)=4, mean(2,3,4)=3
    assert s[0] is None and s[1] is None   # undefined before window fills


def test_rsi2_long_trade_buys_dip_in_uptrend():
    from app.strategies.mean_reversion.rsi2 import simulate_rsi2
    # Gentle uptrend (RSI pinned ~100, no entry), then a mild pullback that stays above
    # the lagging trend SMA (RSI dips below threshold -> entry), then a pop -> exit.
    closes = [10, 11, 12, 13, 14, 15, 14.8, 16]
    bars = _bars(closes)
    out = simulate_rsi2(bars, rsi_period=2, entry=95, exit_sma=2, trend_period=3, slip_bps=0.0)
    assert len(out["trades"]) >= 1
    t = out["trades"][0]
    assert t["entry_price"] in closes                # entered at a close (MOC)
    assert t["exit_reason"] in ("sma_exit", "eod")
    assert "ret" in t and "entry_date" in t and "exit_date" in t
    # daily MTM series exists for the holding window
    assert isinstance(out["daily"], list) and all(len(x) == 2 for x in out["daily"])


def test_rsi2_requires_uptrend_filter():
    from app.strategies.mean_reversion.rsi2 import simulate_rsi2
    # Strict downtrend: close never exceeds the trend SMA -> no longs allowed.
    closes = [20, 19, 18, 17, 16, 15, 14, 13, 12, 11]
    out = simulate_rsi2(_bars(closes), rsi_period=2, entry=99, exit_sma=2, trend_period=3, slip_bps=0.0)
    assert out["trades"] == []
