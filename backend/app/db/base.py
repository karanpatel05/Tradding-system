"""Async SQLAlchemy engine, session factory, and base class.

Supports both SQLite (default, zero-infra local dev) and PostgreSQL (compose /
production) transparently via ``settings.database_url``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# SQLite needs check_same_thread disabled for async use; Postgres ignores it.
_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


async def init_db() -> None:
    """Create all tables. Imports models for their side effect of registering
    them on ``Base.metadata`` before ``create_all`` runs."""
    import app.db.models  # noqa: F401  (registers models on Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_added_columns)
    async with SessionLocal() as session:
        await close_stale_tc_positions_once(session)
        await session.commit()


def _ensure_added_columns(conn) -> None:
    """Minimal additive migration: ``create_all`` never alters existing tables, so
    add columns introduced after a table was first created. Idempotent; safe on
    SQLite and Postgres."""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    tables = set(insp.get_table_names())
    if "trades" in tables:
        existing = {c["name"] for c in insp.get_columns("trades")}
        for col in ("stop", "target"):
            if col not in existing:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col} FLOAT"))
        if "broker_order_id" not in existing:
            conn.execute(text("ALTER TABLE trades ADD COLUMN broker_order_id VARCHAR(64)"))
        if "transmitted" not in existing:
            false = "0" if conn.dialect.name == "sqlite" else "FALSE"  # PG: no int->bool
            conn.execute(text(f"ALTER TABLE trades ADD COLUMN transmitted BOOLEAN DEFAULT {false}"))
        # OCC option symbols need more than the original VARCHAR(16). SQLite does not
        # enforce VARCHAR lengths; Postgres does, so widen it there (idempotent).
        if conn.dialect.name == "postgresql":
            sym = next((c for c in insp.get_columns("trades") if c["name"] == "symbol"), None)
            if sym is not None and getattr(sym["type"], "length", None) not in (None,) \
                    and sym["type"].length < 32:
                conn.execute(text("ALTER TABLE trades ALTER COLUMN symbol TYPE VARCHAR(32)"))
    if "strategy_state" in tables:
        cols = {c["name"] for c in insp.get_columns("strategy_state")}
        if "direction" not in cols:
            conn.execute(text(
                "ALTER TABLE strategy_state ADD COLUMN direction VARCHAR(8) DEFAULT 'both'"))
        if "contracts" not in cols:
            conn.execute(text("ALTER TABLE strategy_state ADD COLUMN contracts INTEGER DEFAULT 1"))
        if "tc_tickers" not in cols:
            conn.execute(text("ALTER TABLE strategy_state ADD COLUMN tc_tickers JSON DEFAULT '[]'"))
    if "option_positions" in tables:
        cols = {c["name"] for c in insp.get_columns("option_positions")}
        for col in ("last_value", "last_pnl_pct", "last_spot"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE option_positions ADD COLUMN {col} FLOAT"))
        if "valued_at" not in cols:
            conn.execute(text("ALTER TABLE option_positions ADD COLUMN valued_at TIMESTAMP"))
        if "contracts" not in cols:
            conn.execute(text("ALTER TABLE option_positions ADD COLUMN contracts INTEGER DEFAULT 1"))
    if "builder_positions" in tables:
        cols = {c["name"] for c in insp.get_columns("builder_positions")}
        if "contracts" not in cols:
            conn.execute(text("ALTER TABLE builder_positions ADD COLUMN contracts INTEGER DEFAULT 1"))


TC_CLEANUP_FLAG = "tc_stale_rows_closed"


async def close_stale_tc_positions_once(session) -> int:
    """Run :func:`close_stale_tc_positions` on the first boot after the Triple Calendar
    removal only, then set a SystemFlag so it never runs again: ``strategy_2`` is a
    reusable slot, and a future options strategy's open rows must survive restarts."""
    from app.db.models import SystemFlag

    if await session.get(SystemFlag, TC_CLEANUP_FLAG) is not None:
        return 0
    n = await close_stale_tc_positions(session)
    session.add(SystemFlag(key=TC_CLEANUP_FLAG, on=True,
                           reason=f"closed {n} stale Triple Calendar row(s)"))
    await session.flush()
    return n


async def close_stale_tc_positions(session) -> int:
    """One-time, idempotent data fix after Triple Calendar removal: close any still-open
    ``strategy_2`` option positions (no manager exists to close them). P&L stays null —
    nothing was actually closed at a price. Returns the number of rows closed."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import OptionPositionRecord

    rows = (await session.execute(select(OptionPositionRecord).where(
        OptionPositionRecord.strategy_key == "strategy_2",
        OptionPositionRecord.status == "open"))).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.status = "closed"
        r.exit_time = now
        r.pnl = None
    await session.flush()
    return len(rows)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        yield session


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone session context for background workers (no FastAPI request)."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
