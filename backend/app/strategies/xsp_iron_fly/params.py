"""XSP 0DTE iron fly parameters (env-driven via Settings, prefix ``XSP_FLY_``).

The strategy rules (entry time, wing width, 1.5x per-leg stop, hold to settlement) are
fixed by the spec; everything else here is execution hygiene or a risk limit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time


def _hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")[:2]
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class IronFlyParams:
    # --- strategy rules (spec; do not add variations) ---
    ticker: str = "XSP"
    entry_time: time = time(9, 45)          # ET
    wing_width: float = 10.0                # index points
    stop_mult: float = 1.5                  # per short leg, x its entry fill
    capital_per_fly: float = 5000.0
    close_before_settlement: bool = False
    flatten_minutes_before_close: float = 2.0   # 15:58 on a 16:00 close

    # --- risk limits / circuit breakers ---
    max_contracts: int = 1                  # flies per day (= max concurrent)
    max_daily_loss: float = 1000.0          # $; also caps sizing (see logic.size_flies)
    flatten_on_daily_loss: bool = False
    max_leg_spread: float = 0.15            # index points, any leg at entry
    max_quote_age: float = 5.0              # seconds, XSP spot at entry
    min_minutes_to_close: float = 240.0     # skips half days (195 min left at 09:45)
    entry_grace_seconds: float = 120.0      # guards re-checked this long, then skip/missed

    # --- execution ---
    mode: str = "sim"                       # "sim" only; "live" is refused (no adapter)
    multiplier: int = 100
    tick: float = 0.01
    fees_per_contract: float = 1.00         # $ per contract per fill (open or close)
    limit_through: float = 0.05             # marketable-limit offset through the touch
    entry_reprice_step: float = 0.05        # combo credit concession per reprice
    entry_reprice_seconds: float = 5.0
    max_entry_concession: float = 0.30      # max credit given up vs the mid, points
    monitor_interval: float = 1.0           # stop-monitor cadence, seconds
    stop_trigger: str = "ask"               # "ask" (buy-to-close price) | "mark"
    close_retry_seconds: float = 1.0
    feed_stale_alert_seconds: float = 30.0
    settlement_delay_seconds: float = 60.0  # wait after the close before reading the print
    settlement_timeout_minutes: float = 60.0

    def validate(self) -> list[str]:
        errs = []
        if self.ticker != "XSP":
            errs.append("ticker must be XSP (cash-settled European index options)")
        if self.wing_width <= 0:
            errs.append("wing_width must be > 0")
        if self.stop_mult <= 1:
            errs.append("stop_mult must be > 1")
        if self.capital_per_fly <= 0:
            errs.append("capital_per_fly must be > 0")
        if self.max_contracts < 1:
            errs.append("max_contracts must be >= 1")
        if self.max_daily_loss <= 0:
            errs.append("max_daily_loss must be > 0")
        if self.stop_trigger not in ("ask", "mark"):
            errs.append("stop_trigger must be 'ask' or 'mark'")
        if self.mode not in ("sim", "live"):
            errs.append("mode must be 'sim' or 'live'")
        return errs

    @classmethod
    def from_settings(cls, s=None) -> "IronFlyParams":
        if s is None:
            from app.config import get_settings
            s = get_settings()
        return cls(
            entry_time=_hhmm(s.xsp_fly_entry_time),
            wing_width=s.xsp_fly_wing_width,
            stop_mult=s.xsp_fly_stop_mult,
            capital_per_fly=s.xsp_fly_capital_per_fly,
            close_before_settlement=s.xsp_fly_close_before_settlement,
            max_contracts=s.xsp_fly_max_contracts,
            max_daily_loss=s.xsp_fly_max_daily_loss,
            flatten_on_daily_loss=s.xsp_fly_flatten_on_daily_loss,
            max_leg_spread=s.xsp_fly_max_leg_spread,
            max_quote_age=s.xsp_fly_max_quote_age,
            min_minutes_to_close=s.xsp_fly_min_minutes_to_close,
            entry_grace_seconds=s.xsp_fly_entry_grace_seconds,
            mode=s.xsp_fly_mode,
            fees_per_contract=s.xsp_fly_fees_per_contract,
            stop_trigger=s.xsp_fly_stop_trigger,
        )
