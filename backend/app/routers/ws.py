"""Realtime WebSocket push channel.

The push channel is gated exactly like REST: the session cookie is validated on
connect and the socket is rejected (1008) when it's missing/invalid. This closes
the easy mistake of gating HTTP but leaving the WS feed open.

Pushes a dashboard snapshot — market status, active strategies, positions — on a
fixed cadence.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sqlalchemy import select

from app.auth.gate import APP_SESSION_COOKIE, is_authenticated
from app.auth.manager import session_manager
from app.config import get_settings
from app.engine.manager import engine_manager
from app.services.market import get_market_status
from app.services.market_stream import market_stream
from app.strategies.registry import list_strategies
from app.db.base import session_scope
from app.db.models import StrategyState, TradeRecord
from app.tastytrade.client import get_client

logger = logging.getLogger("trading.ws")

router = APIRouter()

PUSH_INTERVAL = 2       # seconds between snapshots (prices stream at this cadence)
POSITIONS_EVERY = 5     # refresh positions every Nth tick (they hit the API)


async def _strategies_snapshot() -> list[dict]:
    out = []
    async with session_scope() as session:
        for strat in list_strategies():
            st = await session.get(StrategyState, strat.key)
            out.append({
                "key": strat.key,
                "name": strat.name,
                "ready": strat.ready,
                "status": st.status if st else "idle",
                "selected_symbol": st.selected_symbol if st else None,
                "capital_allocation": st.capital_allocation if st else 0.0,
                "running": engine_manager.is_running(strat.key),
            })
    return out


async def _strategy_positions_snapshot() -> list[dict]:
    """Positions the strategies are *tracking* (open TradeRecords). These exist in
    both paper and live mode — so the dashboard reflects an open trade even when no
    real broker order was sent (ENABLE_LIVE_ORDERS off)."""
    paper = not get_settings().enable_live_orders
    async with session_scope() as session:
        rows = (await session.execute(
            select(TradeRecord).where(TradeRecord.status == "open")
        )).scalars().all()
    return [{
        "symbol": t.symbol,
        "quantity": float(t.quantity or 0),
        "side": "long" if t.side == "buy" else "short",
        "entry_price": t.entry_price,
        "strategy_key": t.strategy_key,
        "source": "paper" if paper else "strategy",
    } for t in rows]


async def _positions_snapshot() -> list[dict]:
    if not session_manager.is_authenticated():
        return []
    try:
        client = get_client()
        out = []
        for account in await client.get_accounts():
            for pos in await client.get_positions(account):
                out.append({
                    "account": account.account_number,
                    "symbol": getattr(pos, "symbol", None),
                    "quantity": float(getattr(pos, "quantity", 0) or 0),
                })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("WS positions fetch failed: %s", exc)
        return []


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    cookie = websocket.cookies.get(APP_SESSION_COOKIE)
    if not is_authenticated(cookie):
        await websocket.close(code=1008)  # policy violation — unauthenticated
        return
    await websocket.accept()

    # Spin up (or reuse) the persistent live-quote stream so the overview below
    # reflects continuously-updating prices, not a per-tick one-shot fetch.
    market_stream.ensure_started()

    tick = 0
    broker_positions: list[dict] = []
    try:
        while True:
            # Broker positions hit the API, so refresh them only every Nth tick;
            # strategy-tracked (incl. paper) positions are cheap DB reads each tick.
            if tick % POSITIONS_EVERY == 0:
                broker_positions = await _positions_snapshot()
            strat_positions = await _strategy_positions_snapshot()
            strat_symbols = {p["symbol"] for p in strat_positions}
            # Strategy positions first; add any broker positions not already tracked
            # (e.g. manually-held symbols) so nothing real is hidden.
            positions = strat_positions + [
                b for b in broker_positions if b.get("symbol") not in strat_symbols
            ]
            await websocket.send_json({
                "type": "snapshot",
                "market_status": get_market_status(),
                "market_overview": market_stream.overview(),  # live, from the stream cache
                "strategies": await _strategies_snapshot(),
                "positions": positions,
            })
            tick += 1
            await asyncio.sleep(PUSH_INTERVAL)
    except WebSocketDisconnect:
        logger.info("WS client disconnected")
    except Exception:  # noqa: BLE001
        logger.exception("WS push loop error")
        await websocket.close(code=1011)
