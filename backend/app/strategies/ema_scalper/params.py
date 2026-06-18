"""Strategy parameters — every value comes from the project config (no hardcoding).

A frozen dataclass so the identical params object can be threaded through the
pure signal core in both backtest and live without accidental mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings


@dataclass(frozen=True)
class EmaScalperParams:
    # signal
    ema_fast: int
    ema_slow: int
    atr_length: int
    candle_interval: str
    slope_lr_lookback: int
    slope_min: float
    slope_lookback: int
    sideways_min_sep_atr: float
    max_entry_distance_atr: float
    stop_buffer_atr: float
    risk_reward: float
    entry_expiry_bars: int
    max_trades_per_day: int
    slippage_pct: float
    # scanner
    watchlist_size: int
    trend_ema_length: int
    min_atr_pct: float
    min_rel_volume: float
    min_gap_pct: float
    min_avg_volume: int
    min_price: float
    use_premarket_filter: bool
    min_premarket_volume: int
    # regime + session
    regime_filter: bool
    regime_symbol: str
    session_start_et: str
    session_cutoff_et: str

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "EmaScalperParams":
        s = settings or get_settings()
        return cls(
            ema_fast=s.ema_fast,
            ema_slow=s.ema_slow,
            atr_length=s.ema_atr_length,
            candle_interval=s.ema_candle_interval,
            slope_lr_lookback=s.ema_slope_lr_lookback,
            slope_min=s.ema_slope_min,
            slope_lookback=s.ema_slope_lookback,
            sideways_min_sep_atr=s.ema_sideways_min_sep_atr,
            max_entry_distance_atr=s.ema_max_entry_distance_atr,
            stop_buffer_atr=s.ema_stop_buffer_atr,
            risk_reward=s.ema_risk_reward,
            entry_expiry_bars=s.ema_entry_expiry_bars,
            max_trades_per_day=s.ema_max_trades_per_day,
            slippage_pct=s.ema_slippage_pct,
            watchlist_size=s.ema_watchlist_size,
            trend_ema_length=s.ema_trend_ema_length,
            min_atr_pct=s.ema_min_atr_pct,
            min_rel_volume=s.ema_min_rel_volume,
            min_gap_pct=s.ema_min_gap_pct,
            min_avg_volume=s.ema_min_avg_volume,
            min_price=s.ema_min_price,
            use_premarket_filter=s.ema_use_premarket_filter,
            min_premarket_volume=s.ema_min_premarket_volume,
            regime_filter=s.ema_regime_filter,
            regime_symbol=s.ema_regime_symbol,
            session_start_et=s.ema_session_start_et,
            session_cutoff_et=s.ema_session_cutoff_et,
        )

    @property
    def warmup_bars(self) -> int:
        """Bars needed before any signal can be computed (EMA15 must exist for a
        full linear-regression window)."""
        return max(self.ema_slow, self.atr_length) + self.slope_lr_lookback + 2
