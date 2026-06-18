"""Risk engine for the multi-position EMA crossover strategy.

Sizing here is **risk-fractional**, not capital/price (that's the scalper's model
in :mod:`app.engine.sizing`). For a candidate entry the engine sizes:

    risk_shares = floor(equity × risk_per_trade_pct / per_share_risk)

where ``per_share_risk = |entry − stop|`` and ``equity`` is the strategy's
allocated capital. Up to ``max_positions`` positions share that one allocation as
a single **buying-power pool**, so the risk-sized quantity is then capped twice:
by the remaining aggregate-risk headroom (``max_portfolio_risk_pct × equity −
Σ open risk``) and by the remaining cash (``equity − Σ open notionals``). A trade
is rejected when a portfolio limit is hit (too many positions, aggregate risk /
daily-loss halt, per-day trade cap) or it can't afford a single share. Capping by
the pool — rather than letting each trade size against the full allocation —
prevents silently running ~``max_positions``× leverage.

The engine holds live in-memory counters keyed to the trading day (US/Eastern).
Daily counters (trades opened, realized P&L) reset on an ET date rollover. On a
worker restart the caller reconstructs open positions via ``register_open`` (with
``count_trade=False``) and the day's realized P&L / trade count via ``seed_day``,
so the limits survive a restart instead of resetting to zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings

ET = ZoneInfo("America/New_York")


def _et_date(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ET).date()


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float
    max_positions: int
    max_portfolio_risk_pct: float
    max_daily_loss_pct: float
    max_trades_per_day: int

    @classmethod
    def from_settings(cls) -> "RiskLimits":
        s = get_settings()
        return cls(
            risk_per_trade_pct=s.xover_risk_per_trade_pct,
            max_positions=s.xover_max_positions,
            max_portfolio_risk_pct=s.xover_max_portfolio_risk_pct,
            max_daily_loss_pct=s.xover_max_daily_loss_pct,
            max_trades_per_day=s.xover_max_trades_per_day,
        )


@dataclass
class RiskSizing:
    quantity: int
    notional: float
    risk_amount: float       # quantity × per-share risk
    rejection: str = ""      # non-empty when quantity == 0 for a real reason


@dataclass
class _OpenPos:
    quantity: int
    notional: float
    risk_amount: float


class RiskEngine:
    """Live risk/sizing state for one multi-position strategy. Not thread-safe;
    driven from a single worker coroutine on one event loop."""

    def __init__(self, equity: float, limits: RiskLimits, *, today: date | None = None) -> None:
        self.equity = equity
        self.limits = limits
        self._day = today or _et_date()
        self._open: dict[str, _OpenPos] = {}
        self._trades_today = 0
        self._realized_today = 0.0

    # --- daily rollover ---------------------------------------------------
    def _roll(self, now: datetime | None = None) -> None:
        d = _et_date(now)
        if d != self._day:
            self._day = d
            self._trades_today = 0
            self._realized_today = 0.0

    # --- derived views ----------------------------------------------------
    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def used_cash(self) -> float:
        return sum(p.notional for p in self._open.values())

    @property
    def used_risk(self) -> float:
        return sum(p.risk_amount for p in self._open.values())

    @property
    def remaining_cash(self) -> float:
        return max(0.0, self.equity - self.used_cash)

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def realized_today(self) -> float:
        return self._realized_today

    def has_position(self, symbol: str) -> bool:
        return symbol in self._open

    def _daily_loss_hit(self) -> bool:
        return self._realized_today <= -abs(self.limits.max_daily_loss_pct) * self.equity

    # --- sizing + admission -----------------------------------------------
    def check_and_size(self, symbol: str, entry: float, stop: float,
                       *, now: datetime | None = None) -> RiskSizing:
        """Risk-size a candidate entry and admit it against the portfolio limits.
        Returns a 0-quantity ``RiskSizing`` with a ``rejection`` reason when the
        trade is blocked; the caller never mutates state on a rejection."""
        self._roll(now)
        if symbol in self._open:
            return RiskSizing(0, 0.0, 0.0, "already holding this symbol")
        if self.equity <= 0:
            return RiskSizing(0, 0.0, 0.0, "no capital allocated")
        per_share = abs(entry - stop)
        if entry <= 0 or per_share <= 0:
            return RiskSizing(0, 0.0, 0.0, "invalid entry/stop")
        if self.open_count >= self.limits.max_positions:
            return RiskSizing(0, 0.0, 0.0, f"max positions ({self.limits.max_positions}) reached")
        if self._trades_today >= self.limits.max_trades_per_day:
            return RiskSizing(0, 0.0, 0.0, f"max trades/day ({self.limits.max_trades_per_day}) reached")
        if self._daily_loss_hit():
            return RiskSizing(0, 0.0, 0.0, "daily loss limit hit — entries halted")

        # 1) risk budget for this single trade.
        qty = int(math.floor(self.equity * self.limits.risk_per_trade_pct / per_share))
        if qty <= 0:
            return RiskSizing(0, 0.0, 0.0, "risk budget too small for one share")

        # 2) cap by remaining aggregate-risk headroom.
        risk_headroom = self.limits.max_portfolio_risk_pct * self.equity - self.used_risk
        if risk_headroom < per_share:
            return RiskSizing(0, 0.0, 0.0, "portfolio risk limit reached")
        qty = min(qty, int(math.floor(risk_headroom / per_share)))

        # 3) cap by remaining cash in the shared buying-power pool.
        if entry > self.remaining_cash:
            return RiskSizing(0, 0.0, 0.0, "insufficient buying power in the pool")
        qty = min(qty, int(math.floor(self.remaining_cash / entry)))
        if qty <= 0:
            return RiskSizing(0, 0.0, 0.0, "insufficient buying power in the pool")

        return RiskSizing(qty, qty * entry, qty * per_share, "")

    # --- state transitions ------------------------------------------------
    def register_open(self, symbol: str, quantity: int, entry: float, stop: float,
                      *, count_trade: bool = True, now: datetime | None = None) -> None:
        """Record a newly-opened position. Pass ``count_trade=False`` when
        reconstructing an existing position on restart (it was already counted)."""
        self._roll(now)
        per_share = abs(entry - stop)
        self._open[symbol] = _OpenPos(quantity, quantity * entry, quantity * per_share)
        if count_trade:
            self._trades_today += 1

    def register_close(self, symbol: str, pnl: float | None, *, now: datetime | None = None) -> None:
        """Drop a position and book its realized P&L into the day's running total."""
        self._roll(now)
        self._open.pop(symbol, None)
        self._realized_today += pnl or 0.0

    def seed_day(self, *, trades_today: int = 0, realized_today: float = 0.0) -> None:
        """Restore today's counters from the DB on a worker restart."""
        self._trades_today = trades_today
        self._realized_today = realized_today

    # --- reporting --------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "open_positions": self.open_count,
            "max_positions": self.limits.max_positions,
            "used_cash": round(self.used_cash, 2),
            "remaining_cash": round(self.remaining_cash, 2),
            "used_risk": round(self.used_risk, 2),
            "max_risk": round(self.limits.max_portfolio_risk_pct * self.equity, 2),
            "trades_today": self._trades_today,
            "max_trades_per_day": self.limits.max_trades_per_day,
            "realized_today": round(self._realized_today, 2),
            "daily_loss_limit": round(-abs(self.limits.max_daily_loss_pct) * self.equity, 2),
            "halted": self._daily_loss_hit(),
        }
