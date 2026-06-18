"""Small deterministic fixture dataset for the EMA scalper tests.

Bars are dicts shaped like ``client.get_candles`` output: float OHLC + volume and
an epoch-ms ``time`` (5-minute spacing). The intraday builder constructs a clean
long setup (trend → shallow pullback to the EMAs → bullish confirmation → breakout
→ run to target) computing the EMAs as it goes so the geometry actually satisfies
the signal conditions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.strategies.ema_scalper import indicators as ind

_FIVE_MIN_MS = 5 * 60 * 1000
# Base = 2024-06-03 (Mon) 09:35 America/New_York = 13:35 UTC. 5-min bars from here
# stay inside the regular session, so the session filter doesn't block the fixtures.
_BASE_MS = int(datetime(2024, 6, 3, 13, 35, tzinfo=timezone.utc).timestamp() * 1000)


def _bar(i: int, o: float, h: float, l: float, c: float, v: int = 2_000_000) -> dict:
    return {"time": _BASE_MS + i * _FIVE_MIN_MS, "open": round(o, 4), "high": round(h, 4),
            "low": round(l, 4), "close": round(c, 4), "volume": v, "symbol": "TEST"}


def long_setup_bars() -> list[dict]:
    """Intraday 5-min bars containing exactly one clean long setup that reaches
    its target, satisfying every gate (trend, EMA15 LR-slope ≥ 0.20·ATR, EMA
    separation, ≤ 2·ATR entry distance, pullback hammer into the zone)."""
    bars: list[dict] = []
    closes: list[float] = []
    i = 0
    price = 100.0

    def push(o, h, l, c):
        nonlocal i
        bars.append(_bar(i, o, h, l, c))
        closes.append(c)
        i += 1

    # 1) Warmup uptrend with wide ranges (high ATR keeps the 2·ATR entry-distance
    #    window open while a strong EMA15 slope clears the threshold).
    for _ in range(40):
        o = price
        c = price + 0.6
        push(o, c + 0.7, o - 0.7, c)   # body 0.6, range ~2.0 → ATR ≈ 2
        price = c

    # 2) Shallow one-bar pullback (keeps EMA9 > EMA15).
    o = price
    c = price - 0.6
    push(o, o + 0.2, c - 0.2, c)
    price = c

    # 3) Bullish hammer confirmation: long lower wick into the EMA zone, close back
    #    above EMA15 but within 2·ATR of it.
    ef = ind.ema(closes, 9)[-1]
    es = ind.ema(closes, 15)[-1]
    atr = ind.atr(bars, 14)[-1]
    zone_hi = max(ef, es)
    c = es + min(1.0 * atr, 2.0 * atr - 0.5)   # close above EMA15, within 2·ATR
    o = c - 0.3                                  # small body
    l = zone_hi - 1.0                            # long lower wick dips into the zone
    h = c + 0.1
    push(o, h, l, c)
    conf_high = h
    price = c

    # 4) Breakout bar trades through the confirmation high → entry fills there.
    o = price
    push(o, conf_high + 1.5, o - 0.2, conf_high + 1.0)
    price = conf_high + 1.0

    # 5) Continue up to hit the 1:2 target.
    for _ in range(16):
        o = price
        c = price + 0.6
        push(o, c + 0.7, o - 0.7, c)
        price = c

    return bars


def flat_chop_bars() -> list[dict]:
    """Sideways chop — no trend, no qualifying setup (signal must stay flat)."""
    bars: list[dict] = []
    base = 50.0
    for i in range(60):
        o = base + (0.1 if i % 2 else -0.1)
        c = base - (0.1 if i % 2 else -0.1)
        bars.append(_bar(i, o, max(o, c) + 0.05, min(o, c) - 0.05, c))
    return bars


# --- daily scanner fixtures ---------------------------------------------
def trending_daily_bars(symbol: str = "MOVR", n: int = 60) -> list[dict]:
    """A high-volatility, rising, liquid name that should pass every filter
    (price ≥ $10, avg vol ≥ 1M, RVOL ≥ 2, gap ≥ 2%, ATR ≥ 2%)."""
    bars: list[dict] = []
    price = 80.0
    for i in range(n):
        last = i == n - 1
        # Final session gaps up ≥2% and prints ≥2× average volume (in-play).
        o = price * 1.03 if last else price
        c = o + 1.0
        h, l = c + 1.5, o - 1.5             # ~3-wide range → ATR ≈ 3 (>2%)
        vol = 5_000_000 if last else 2_000_000
        bars.append({"time": i * 86_400_000, "open": o, "high": h, "low": l,
                     "close": c, "volume": vol, "symbol": symbol})
        price = c
    return bars


def quiet_daily_bars(symbol: str = "CALM", n: int = 60) -> list[dict]:
    """Low-volatility, low-volume name that should fail the ATR%/liquidity floors."""
    bars: list[dict] = []
    price = 100.0
    for i in range(n):
        o = price
        c = price + 0.02
        bars.append({"time": i * 86_400_000, "open": o, "high": c + 0.03,
                     "low": o - 0.03, "close": c, "volume": 100_000, "symbol": symbol})
        price = c
    return bars
