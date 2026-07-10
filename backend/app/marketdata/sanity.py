"""Option-mid sanity guard: a suspect quote becomes None (skip this tick), never a number.
Exists because a garbage IBKR mid once produced a $58 entry on a ~$4 calendar spread."""
from __future__ import annotations

import time


class MidSanity:
    def __init__(self, rel_spread: float | None = None, abs_spread: float | None = None,
                 max_jump_pct: float | None = None, window_seconds: int | None = None) -> None:
        from app.config import get_settings
        s = get_settings()
        self.rel_spread = rel_spread if rel_spread is not None else s.sanity_rel_spread
        self.abs_spread = abs_spread if abs_spread is not None else s.sanity_abs_spread
        self.max_jump_pct = max_jump_pct if max_jump_pct is not None else s.sanity_max_jump_pct
        self.window_seconds = (window_seconds if window_seconds is not None
                               else s.sanity_jump_window_seconds)
        self._last: dict[str, tuple[float, float]] = {}   # key -> (accepted mid, monotime)

    def check(self, key: str, bid: float | None, ask: float | None,
              now: float | None = None) -> tuple[float | None, str | None]:
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None, "missing"
        if ask < bid:
            return None, "crossed"
        mid = (bid + ask) / 2
        if (ask - bid) > max(self.abs_spread, self.rel_spread * mid):
            return None, "wide_spread"
        t = now if now is not None else time.monotonic()
        last = self._last.get(key)
        if last is not None:
            last_mid, last_t = last
            if (t - last_t) <= self.window_seconds and last_mid > 0 \
                    and abs(mid - last_mid) / last_mid > self.max_jump_pct:
                return None, "jump"           # memory keeps the ACCEPTED mid
        self._last[key] = (mid, t)
        return mid, None
