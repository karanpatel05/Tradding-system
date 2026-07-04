"""Triple Calendar config — every threshold named, none hardcoded in the logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class TripleCalendarParams:
    underlying: str = "QQQ"
    sell_dte: int = 21
    buy_dte: int = 28
    strike_round: float = 5.0
    em_buffer: float = 5.0          # added to expected move before placing outer strikes
    manual_nudge: float = 0.0       # optional $ to widen outer strikes (toward S/R)
    profit_target: float = 0.10     # close at +10% of net debit
    stop_loss_pct: float = 0.25     # close at -25% of net debit
    vix_threshold: float = 13.0     # below this, scale size down (don't skip)
    vix_scale: float = 0.5
    vix_entry_max: float = 16.0      # hard gate: no entry when VIX is above this (wait for it to fall)
    max_calendars: int = 4
    allow_fifth: bool = False
    risk_free_rate: float = 0.04
    div_yield: float = 0.0
    event_dates: list[date] = field(default_factory=list)
