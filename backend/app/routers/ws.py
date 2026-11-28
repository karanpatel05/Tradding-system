"""The dashboard WebSocket sends account and strategy snapshots on a fixed cadence.
The public market WebSocket streams only the requested symbols' time-and-sales data.
"""

from __future__ import annotations

import asyncio
import logging

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sqlalchemy import select

from app.auth.gate import APP_SESSION_COOKIE, is_authenticated
from app.config import get_settings
from app.engine.manager import engine_manager
from app.services.alpaca_status import alpaca_ready_cached
from app.services.chart_data import ChartDataError, parse_symbols
from app.services.market import get_market_status
from app.services.market_stream import market_stream
from app.strategies.registry import list_strategies
from app.db.base import session_scope
from app.db.models import StrategyState, TradeRecord
from app.broker.account import balances as get_account_summary, positions as get_positions
from app.tastytrade.streamer import get_streamer, TickSubscription

logger = logging.getLogger("trading.ws")

router = APIRouter()

PUSH_INTERVAL = 2       # seconds between portfolio snapshots
POSITIONS_EVERY = 5     # refresh positions every Nth tick (they hit the API)


async def _send_market_ticks(websocket: WebSocket, subscription: TickSubscription) -> None:
    if not subscription.gapped:
        await websocket.send_json({"type": "status", "connected": True})
        while batch := await subscription.next_batch():
            await websocket.send_json({"type": "ticks", "ticks": batch})
    await websocket.send_json({"type": "status", "connected": False})
    await websocket.close(code=1013, reason="Market tick stream interrupted")


@router.websocket("/ws/market")
async def market_ticks(websocket: WebSocket, symbols: str = "") -> None:
    """Public time-and-sales stream for the chart and market overview."""
    try:
        requested = parse_symbols(symbols)
    except ChartDataError:
        await websocket.close(code=1008, reason="Invalid market symbol list")
        return

    await websocket.accept()
    async with get_streamer().listen_ticks(requested) as subscription:
        async with anyio.create_task_group() as group:
            async def publish():
                try:
                    await _send_market_ticks(websocket, subscription)
                except WebSocketDisconnect:
                    pass
                finally:
                    group.cancel_scope.cancel()

            group.start_soon(publish)
            # The browser can leave long before a quiet symbol trades again.
            try:
                while (await websocket.receive())["type"] != "websocket.disconnect":
                    pass
            finally:
                group.cancel_scope.cancel()


async def _strategies_snapshot() -> list[dict]:
    out = []
    async with session_scope() as session:
        for strat in list_strategies():
            if strat.archived and not engine_manager.is_running(strat.key):
                continue
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
    if not alpaca_ready_cached():
        return []
    try:
        return [{"account": p.account, "symbol": p.symbol, "quantity": float(p.quantity)}
                for p in await get_positions()]
    except Exception as exc:  # noqa: BLE001
        logger.debug("WS positions fetch failed: %s", exc)
        return []


async def _account_snapshot() -> dict:
    if not alpaca_ready_cached():
        return {}
    try:
        return await get_account_summary()
    except Exception as exc:  # noqa: BLE001
        logger.debug("WS account fetch failed: %s", exc)
        return {}


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
    broker_account: dict = {}
    try:
        while True:
            market_stream.keep_alive()             # resubscribe after a feed drop
            # Broker positions hit the API, so refresh them only every Nth tick;
            # strategy-tracked (incl. paper) positions are cheap DB reads each tick.
            if tick % POSITIONS_EVERY == 0:
                broker_positions = await _positions_snapshot()
                broker_account = await _account_snapshot()
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
                "market_overview": market_stream.snapshot(),  # live, from the stream cache
                "strategies": await _strategies_snapshot(),
                "positions": positions,
                "account": broker_account,
            })
            tick += 1
            await asyncio.sleep(PUSH_INTERVAL)
    except WebSocketDisconnect:
        logger.info("WS client disconnected")
    except Exception:  # noqa: BLE001
        logger.exception("WS push loop error")
        await websocket.close(code=1011)
