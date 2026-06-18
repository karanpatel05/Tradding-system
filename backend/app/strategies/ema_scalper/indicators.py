"""Pure technical indicators + candle-pattern detectors.

No I/O, no global state — every function is a deterministic transform over plain
floats / OHLC dicts so they can be unit-tested directly and produce identical
output in backtest and live. A "bar" is a dict with float ``open/high/low/close``
and ``volume`` (the shape returned by ``client.get_candles``).
"""

from __future__ import annotations

Bar = dict


def ema(values: list[float], length: int) -> list[float | None]:
    """EMA series aligned to ``values``; None until ``length`` samples exist.

    Seeded with the SMA of the first ``length`` values, then the standard
    2/(length+1) multiplier."""
    if length <= 0:
        raise ValueError("length must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    k = 2.0 / (length + 1)
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_ranges(bars: list[Bar]) -> list[float | None]:
    """True range per bar; index 0 is None (needs a previous close)."""
    trs: list[float | None] = [None]
    for i in range(1, len(bars)):
        h, l = bars[i]["high"], bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(bars: list[Bar], length: int) -> list[float | None]:
    """Wilder's ATR aligned to ``bars``; None until ``length`` true ranges exist."""
    if length <= 0:
        raise ValueError("length must be positive")
    trs = true_ranges(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= length:
        return out
    first = sum(t for t in trs[1 : length + 1]) / length  # noqa: E203
    out[length] = first
    prev = first
    for i in range(length + 1, len(bars)):
        prev = (prev * (length - 1) + trs[i]) / length
        out[i] = prev
    return out


def linreg_slope(values: list[float]) -> float:
    """Least-squares slope of ``values`` against index 0..n-1 (price per bar).

    An objective trend-strength measure — no visual angles. 0.0 for <2 points."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


# --- candle anatomy ------------------------------------------------------
def _body(b: Bar) -> float:
    return abs(b["close"] - b["open"])


def _range(b: Bar) -> float:
    return b["high"] - b["low"]


def is_bull(b: Bar) -> bool:
    return b["close"] > b["open"]


def is_bear(b: Bar) -> bool:
    return b["close"] < b["open"]


def _upper_wick(b: Bar) -> float:
    return b["high"] - max(b["open"], b["close"])


def _lower_wick(b: Bar) -> float:
    return min(b["open"], b["close"]) - b["low"]


def bullish_hammer(b: Bar) -> bool:
    """Long lower wick, small upper wick — buyers rejected the lows."""
    rng = _range(b)
    if rng <= 0:
        return False
    body = _body(b)
    return _lower_wick(b) >= 2 * body and _upper_wick(b) <= body and b["close"] >= b["open"]


def bearish_star(b: Bar) -> bool:
    """Mirror of the hammer — long upper wick, sellers rejected the highs."""
    rng = _range(b)
    if rng <= 0:
        return False
    body = _body(b)
    return _upper_wick(b) >= 2 * body and _lower_wick(b) <= body and b["close"] <= b["open"]


def bullish_engulfing(prev: Bar, b: Bar) -> bool:
    return is_bear(prev) and is_bull(b) and b["close"] >= prev["open"] and b["open"] <= prev["close"]


def bearish_engulfing(prev: Bar, b: Bar) -> bool:
    return is_bull(prev) and is_bear(b) and b["close"] <= prev["open"] and b["open"] >= prev["close"]


def strong_bull_body(b: Bar, frac: float = 0.7) -> bool:
    rng = _range(b)
    return rng > 0 and is_bull(b) and _body(b) >= frac * rng


def strong_bear_body(b: Bar, frac: float = 0.7) -> bool:
    rng = _range(b)
    return rng > 0 and is_bear(b) and _body(b) >= frac * rng


def bullish_confirmation(prev: Bar | None, b: Bar) -> bool:
    return strong_bull_body(b) or bullish_hammer(b) or (prev is not None and bullish_engulfing(prev, b))


def bearish_confirmation(prev: Bar | None, b: Bar) -> bool:
    return strong_bear_body(b) or bearish_star(b) or (prev is not None and bearish_engulfing(prev, b))
