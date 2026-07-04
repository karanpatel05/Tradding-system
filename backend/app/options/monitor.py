"""Background monitor for builder (tracked paper) trades: revalue open trades from live mids,
persist P&L, and paper-close on target/stop. NEVER transmits orders."""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.ibkr.gateway import ConnState, get_gateway
from app.options import store
from app.options.chain import get_leg_mids
from app.options.combo import combo_value, exit_check
from app.services.journal import log_event
from app.services.market import get_market_status

logger = logging.getLogger(__name__)


async def value_open_trades_once(mid_source) -> None:
    """One valuation pass over all open trades. mid_source(underlying, legs) -> list[mid]."""
    for t in await store.list_open():
        try:
            mids = await mid_source(t.underlying, t.legs)
            value = combo_value(t.legs, mids)
            if value is None:
                continue  # a mid was unavailable this tick; leave prior valuation
            pnl = round((value - t.net_debit) * 100, 2)
            pnl_pct = (value - t.net_debit) / t.net_debit if t.net_debit else 0.0
            await store.update_valuation(t.id, value=round(value, 4), pnl=pnl,
                                         pnl_pct=round(pnl_pct, 6))
            reason = exit_check(t.net_debit, value, t.target_pct, t.stop_pct)
            if reason:
                await store.close_trade(t.id, pnl=pnl, reason=reason)
                await log_event(f"Builder trade {t.underlying} paper-closed ({reason}) "
                                f"P&L ${pnl:.2f}", category="trade")
        except Exception:  # noqa: BLE001 — never let one trade kill the pass
            logger.exception("builder valuation failed for trade %s", t.id)


async def run_builder_monitor() -> None:
    """App-lifespan loop. Only values when READY + regular market open (avoid stale-quote
    exits). Durable across restart — state lives in the table."""
    await log_event("Builder monitor started", category="action")
    while True:
        interval = get_settings().builder_monitor_interval_seconds
        try:
            if (get_gateway().state is ConnState.READY
                    and get_market_status().get("is_open")):
                await value_open_trades_once(get_leg_mids)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("builder monitor tick error")
        await asyncio.sleep(interval)
