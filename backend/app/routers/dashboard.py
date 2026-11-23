"""Dashboard routes: market status/overview, positions, and the kill switch.

Market status is intentionally **ungated** (it's just a clock) so the login
screen can show the open/closed indicator. Everything that touches the account
or the engine requires auth.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from app.auth.gate import require_auth
from app.history.service import DEFAULT_LIMIT, HistoryRequestError, get_history
from app.history.sync import history_status
from app.services.chart_data import ChartDataError, get_bars, get_quotes
from app.engine.execution import flatten_all
from app.engine.manager import engine_manager
from app.engine.worker import close_open_trades
from app.broker.account import balances as get_account_summary, positions as get_positions
from app.services.alpaca_status import alpaca_ready_cached
from app.services.market import get_market_overview, get_market_status

logger = logging.getLogger("trading.dashboard")

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/market/status")
async def market_status() -> dict:
    """Ungated live open/closed indicator."""
    return get_market_status()


@router.get("/market/overview")
async def market_overview() -> dict:
    """Ungated public market data (index ETF quotes). Account routes stay gated."""
    return await get_market_overview()


@router.get("/market/bars")
async def market_bars(symbol: str, interval: str = "5m") -> dict:
    """Candle history for the Chart screen (1m, 5m, 15m or 1d). Empty with a note when
    the feed has nothing; the frontend then falls back to labelled sample data."""
    try:
        return await get_bars(symbol, interval)
    except ChartDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/market/history/status")
async def market_history_status() -> dict:
    """Per-symbol coverage and sync state of the local price history."""
    return await asyncio.to_thread(history_status)


@router.get("/market/history")
async def market_history(symbol: str, interval: str = "5m", before: int | None = None,
                         after: int | None = None, around: date | None = None,
                         limit: int = DEFAULT_LIMIT) -> dict:
    """Paged deep history for the Chart screen: newest page, or one page before/after a
    bar time, or centred on a date (snapped to the nearest available bars)."""
    try:
        return await get_history(symbol, interval, before=before, after=after,
                                 around=around, limit=limit)
    except HistoryRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/market/quotes")
async def market_quotes(symbols: str) -> dict:
    """Watchlist quotes: last price and change from the previous close."""
    try:
        return await get_quotes(symbols)
    except ChartDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/positions", dependencies=[Depends(require_auth)])
async def positions() -> dict:
    """Alpaca paper positions (shared account). Degrades gracefully."""
    if not alpaca_ready_cached():
        return {"positions": [], "note": "Alpaca not connected."}
    out: list[dict] = []
    try:
        for p in await get_positions():
            out.append({
                "account": p.account,
                "symbol": p.symbol,
                "quantity": _num(p.quantity),
                "instrument_type": p.instrument_type,
                "average_open_price": _num(p.avg_price),
                "close_price": None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Position fetch failed: %s", exc)
        return {"positions": [], "note": "Position feed temporarily unavailable."}
    return {"positions": out, "note": None}


@router.get("/account", dependencies=[Depends(require_auth)])
async def account() -> dict:
    """Alpaca paper account summary (shared account). Degrades gracefully."""
    if not alpaca_ready_cached():
        return {"account": {}, "note": "Alpaca not connected."}
    try:
        return {"account": await get_account_summary(), "note": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Account summary fetch failed: %s", exc)
        return {"account": {}, "note": "Account feed temporarily unavailable."}


@router.post("/kill", dependencies=[Depends(require_auth)])
async def kill_switch() -> dict:
    """Immediately cancel all strategy workers and flatten all positions.

    Idempotent and partial-failure-aware (see EngineManager.kill_all). Returns a
    structured report so the UI can show whether exposure actually reached zero.
    """
    report = await engine_manager.kill_all(flatten=flatten_all)
    # Workers are awaited-to-cancellation inside kill_all, so no live worker can
    # re-open a trade here. Close the ledger so the dashboard (which reads open
    # TradeRecords) actually goes flat — broker flatten alone leaves paper-mode
    # positions, and the ledger row, standing.
    result = report.to_dict()
    result["trades_closed"] = await close_open_trades()
    result["strategies_stopped"] = await _mark_strategies_stopped()
    return result


async def _mark_strategies_stopped() -> list[str]:
    """The kill switch STOPS strategies: persist status "stopped" so the boot-time
    auto-resume (which restarts scanning/active/paused rows) does not bring a killed
    strategy back after the next process restart."""
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import StrategyState
    async with session_scope() as session:
        rows = (await session.execute(select(StrategyState).where(
            StrategyState.status.in_(("scanning", "active", "paused"))))).scalars().all()
        for st in rows:
            st.status = "stopped"
        return [st.key for st in rows]


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
