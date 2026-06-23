"""Dashboard routes: market status/overview, positions, and the kill switch.

Market status is intentionally **ungated** (it's just a clock) so the login
screen can show the open/closed indicator. Everything that touches the account
or the engine requires auth.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.gate import require_auth
from app.auth.manager import session_manager
from app.engine.execution import flatten_all
from app.engine.manager import engine_manager
from app.engine.worker import close_open_trades
from app.ibkr.account import get_account_summary, get_positions
from app.services.market import get_market_overview, get_market_status

logger = logging.getLogger("trading.dashboard")

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/market/status")
async def market_status() -> dict:
    """Ungated live open/closed indicator."""
    return get_market_status()


@router.get("/market/overview", dependencies=[Depends(require_auth)])
async def market_overview() -> dict:
    return await get_market_overview()


@router.get("/positions", dependencies=[Depends(require_auth)])
async def positions() -> dict:
    """Live view of all open positions across accounts. Degrades gracefully."""
    if not session_manager.is_authenticated():
        return {"positions": [], "note": "Not connected to IBKR."}
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
    """IBKR account summary (balances/PnL/margin). Degrades gracefully."""
    if not session_manager.is_authenticated():
        return {"account": {}, "note": "Not connected to IBKR."}
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
    return result


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
