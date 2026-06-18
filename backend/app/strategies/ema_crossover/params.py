"""Immutable parameter bundle for the 9/15 EMA crossover strategy.

Built from ``config.Settings`` (env-driven) so nothing is hardcoded; the backtest
config / optimizer override fields via ``dataclasses.replace``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class EmaCrossoverParams:
    fast: int
    slow: int
    atr_length: int
    candle_interval: str
    vol_avg_window: int
    vol_multiplier: float
    slope_lookback: int
    stop_atr_mult: float
    risk_reward: float
    use_htf_filter: bool
    htf_interval: str
    htf_fast: int
    htf_slow: int
    # Universe filters (eligibility).
    min_price: float
    min_avg_volume: int
    min_rel_volume: float
    min_atr_pct: float
    # RTH session gate (ET). Entries only within [start, cutoff); force-flat at close.
    use_rth_filter: bool = True
    session_start_et: str = "09:30"
    session_cutoff_et: str = "15:55"
    session_close_et: str = "16:00"

    @property
    def warmup_bars(self) -> int:
        """Bars needed before any signal: longest EMA/ATR/vol window + slope lookback."""
        return max(self.slow, self.atr_length, self.vol_avg_window) + self.slope_lookback + 2

    @classmethod
    def from_settings(cls) -> "EmaCrossoverParams":
        s = get_settings()
        return cls(
            fast=s.xover_fast,
            slow=s.xover_slow,
            atr_length=s.xover_atr_length,
            candle_interval=s.xover_candle_interval,
            vol_avg_window=s.xover_vol_avg_window,
            vol_multiplier=s.xover_vol_multiplier,
            slope_lookback=s.xover_slope_lookback,
            stop_atr_mult=s.xover_stop_atr_mult,
            risk_reward=s.xover_risk_reward,
            use_htf_filter=s.xover_use_htf_filter,
            htf_interval=s.xover_htf_interval,
            htf_fast=s.xover_htf_fast,
            htf_slow=s.xover_htf_slow,
            min_price=s.xover_min_price,
            min_avg_volume=s.xover_min_avg_volume,
            min_rel_volume=s.xover_min_rel_volume,
            min_atr_pct=s.xover_min_atr_pct,
            use_rth_filter=s.xover_use_rth_filter,
            session_start_et=s.xover_session_start_et,
            session_cutoff_et=s.xover_session_cutoff_et,
            session_close_et=s.xover_session_close_et,
        )
