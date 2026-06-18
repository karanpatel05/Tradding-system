"""Session-window + regime helpers, shared by backtest and live.

Both modes derive the same per-bar flags (entry window open? past cutoff? regime
allows long/short?) from bar timestamps and the regime symbol's bars, so the flags
fed into ``generate_signal`` are computed identically everywhere.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app.strategies.ema_scalper import indicators as ind
from app.strategies.ema_scalper.params import EmaScalperParams

ET = ZoneInfo("America/New_York")


def _et(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ET)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_session_open(ms: int | None, params: EmaScalperParams) -> bool:
    """True if the bar's ET time is within [start, cutoff) — entries allowed."""
    dt = _et(ms)
    if dt is None:
        return False
    t = dt.time()
    return _parse_hhmm(params.session_start_et) <= t < _parse_hhmm(params.session_cutoff_et)


def is_past_cutoff(ms: int | None, params: EmaScalperParams) -> bool:
    """True at/after the flat-everything cutoff (no overnight holds)."""
    dt = _et(ms)
    if dt is None:
        return False
    return dt.time() >= _parse_hhmm(params.session_cutoff_et)


def _regime_states(regime_bars: list[dict], params: EmaScalperParams) -> list[tuple[int, bool, bool]]:
    """(time, allow_long, allow_short) per regime bar: long allowed when the regime
    symbol is above both its EMAs, short when below."""
    closes = [b["close"] for b in regime_bars]
    ef = ind.ema(closes, params.ema_fast)
    es = ind.ema(closes, params.ema_slow)
    out: list[tuple[int, bool, bool]] = []
    for i, b in enumerate(regime_bars):
        if ef[i] is None or es[i] is None:
            out.append((b.get("time") or 0, True, True))  # warmup: don't block
            continue
        above = b["close"] > ef[i] and b["close"] > es[i]
        below = b["close"] < ef[i] and b["close"] < es[i]
        out.append((b.get("time") or 0, above, below))
    return out


def regime_now(regime_bars: list[dict], params: EmaScalperParams) -> tuple[bool, bool]:
    """Latest regime gate (live). (allow_long, allow_short)."""
    if not params.regime_filter or len(regime_bars) <= params.ema_slow:
        return True, True
    _, allow_long, allow_short = _regime_states(regime_bars, params)[-1]
    return allow_long, allow_short


def regime_series(symbol_bars: list[dict], regime_bars: list[dict],
                  params: EmaScalperParams) -> tuple[list[bool], list[bool]]:
    """Per-bar regime gates aligned to ``symbol_bars`` by timestamp (backtest).

    Each symbol bar uses the most recent regime bar at-or-before its time — no
    lookahead. Returns (allow_long[], allow_short[]) the length of symbol_bars."""
    n = len(symbol_bars)
    if not params.regime_filter or not regime_bars:
        return [True] * n, [True] * n

    states = _regime_states(regime_bars, params)
    times = [s[0] for s in states]
    allow_long = [True] * n
    allow_short = [True] * n

    # Two-pointer walk (both sorted ascending by time).
    j = 0
    for i, b in enumerate(symbol_bars):
        ts = b.get("time") or 0
        while j + 1 < len(states) and times[j + 1] <= ts:
            j += 1
        if times and times[j] <= ts:
            allow_long[i] = states[j][1]
            allow_short[i] = states[j][2]
    return allow_long, allow_short


def session_series(bars: list[dict], params: EmaScalperParams) -> tuple[list[bool], list[bool]]:
    """Per-bar (can_open[], must_flat[]) for the backtest replay."""
    can_open = [is_session_open(b.get("time"), params) for b in bars]
    must_flat = [is_past_cutoff(b.get("time"), params) for b in bars]
    return can_open, must_flat
