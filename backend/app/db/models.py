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
