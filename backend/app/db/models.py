"""ORM models: strategy state, trades, logs, journal notes.

Portable across SQLite (dev) and PostgreSQL (prod) — uses the generic ``JSON``
type and timezone-aware datetimes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrategyState(Base):
    """Runtime state for each of the 5 strategies (the definition lives in the
    code registry; this row holds mutable state)."""

    __tablename__ = "strategy_state"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. "strategy_1"
    name: Mapped[str] = mapped_column(String(128))
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    # idle | scanning | active | stopped
    status: Mapped[str] = mapped_column(String(16), default="idle")
    selected_symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    capital_allocation: Mapped[float] = mapped_column(Float, default=0.0)
    # Trade-direction mode for the multi-position crossover: long | short | both.
    # Gates which sides may OPEN new positions (existing positions ride their own
    # exits). Backtest accepts the same modes via the ``direction`` override.
    direction: Mapped[str] = mapped_column(String(8), default="both")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TradeRecord(Base):
    """A single trade (open and, once closed, exit/P&L). Calendar P&L aggregates
    these by the date of ``exit_time``."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_key: Mapped[str] = mapped_column(String(32), index=True)
    account_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))  # buy | sell
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)    # SL set at entry
    target: Mapped[float | None] = mapped_column(Float, nullable=True)  # TP set at entry
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    # Broker bracket (OTOCO) complex-order id, so a live restart can re-link the
    # resting stop/target to this trade. None in paper mode.
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LogEntry(Base):
    """Structured log of every action / signal / trade / decision."""

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    # action | signal | trade | decision | system
    category: Mapped[str] = mapped_column(String(16), default="action", index=True)
    strategy_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class JournalNote(Base):
    """Free-text note attached to a calendar day."""

    __tablename__ = "journal_notes"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class OptionPositionRecord(Base):
    """A live multi-leg options position (e.g. Triple Calendar), JSON-serialized so a
    restart can resume management. One open record per strategy at a time."""

    __tablename__ = "option_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_key: Mapped[str] = mapped_column(String(32), index=True)
    underlying: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    paper: Mapped[bool] = mapped_column(Boolean, default=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    net_debit: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    calendars: Mapped[list] = mapped_column(JSON, default=list)   # [{strike, sell, buy}]
    order_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Live valuation, refreshed by the manage loop each tick (no extra IBKR calls — it
    # reuses the value it already computes for target/stop). Null until first valued.
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    valued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BuilderPositionRecord(Base):
    """A user-built, tracked-only (paper) multi-leg options trade. No orders are ever
    transmitted; a monitor revalues it from live mids and paper-closes on target/stop."""

    __tablename__ = "builder_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(16))
    legs: Mapped[list] = mapped_column(JSON, default=list)   # canonical leg dicts
    net_debit: Mapped[float] = mapped_column(Float, default=0.0)  # per 1x; debit>0, credit<0
    iv: Mapped[float] = mapped_column(Float, default=0.25)   # IV used for the saved payoff
    target_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(8), nullable=True)  # target|stop|manual
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)          # realized $ at close
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)   # per 1x
    last_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)     # $ unrealized
    last_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    valued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemFlag(Base):
    """Tiny key/value flags for engine-wide state (e.g. safe_mode)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    on: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class OrderIntent(Base):
    """Durable record of an order we intend to / did transmit, written BEFORE
    transmit so a reconnect dedupes by ref/permId instead of resubmitting."""

    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    strategy_key: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    intent: Mapped[str] = mapped_column(String(16))      # enter_long | exit_long | ...
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    perm_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # submitting | working | filled | cancelled | rejected
    status: Mapped[str] = mapped_column(String(12), default="submitting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkerState(Base):
    """Durable snapshot of a worker's in-memory State so a restart resumes mid-trade."""

    __tablename__ = "worker_state"

    strategy_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    managed_symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    armed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    open_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_bar_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
