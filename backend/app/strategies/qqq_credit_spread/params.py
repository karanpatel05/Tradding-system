"""Settings for the in-app QQQ spread (env-driven, see ``app.config``)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QqqSpreadParams:
    model_dir: str
    max_model_age_days: int = 120
    max_contracts: int = 1
    capital_per_spread: float = 1000.0
    first_spread_capital: float = 500.0
    min_credit: float = 0.02
    reprice_seconds: float = 15.0
    grace_minutes: int = 10
    final_close_seconds: int = 600
    close_deadline_seconds: int = 300

    @classmethod
    def from_settings(cls) -> "QqqSpreadParams":
        from app.config import get_settings
        s = get_settings()
        return cls(model_dir=s.qqq_spread_model_dir,
                   max_model_age_days=s.qqq_spread_max_model_age_days,
                   max_contracts=s.qqq_spread_max_contracts,
                   capital_per_spread=s.qqq_spread_capital_per_spread,
                   first_spread_capital=s.qqq_spread_first_spread_capital,
                   min_credit=s.qqq_spread_min_credit,
                   reprice_seconds=s.qqq_spread_reprice_seconds,
                   grace_minutes=s.qqq_spread_grace_minutes,
                   final_close_seconds=s.qqq_spread_final_close_seconds,
                   close_deadline_seconds=s.qqq_spread_close_deadline_seconds)

    def validate(self) -> list[str]:
        errs = []
        if self.max_contracts < 0:
            errs.append("max_contracts must be >= 0")
        if self.capital_per_spread <= 0 or self.first_spread_capital <= 0:
            errs.append("capital steps must be positive")
        if self.reprice_seconds < 1:
            errs.append("reprice_seconds must be >= 1")
        if not 0 < self.close_deadline_seconds < self.final_close_seconds:
            errs.append("need 0 < close_deadline_seconds < final_close_seconds")
        if self.final_close_seconds >= 1800:
            errs.append("final_close_seconds must be < 1800 (after the 30-min checkpoint)")
        return errs
