"""Market-data continuity: a pure gap gate + a best-effort proactive backfill that
force-refreshes active strategies' bars after a reconnect (reuses the IBKR historical
provider + history_cache)."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import StrategyState
from app.providers import get_historical_provider
from app.services import history_cache
from app.strategies.registry import get_strategy

logger = logging.getLogger("trading.backfill")

_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def interval_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(interval, 300)


def is_stale(bars: list[dict], interval_seconds: int, now_ms: int, *, market_open: bool) -> bool:
    if not market_open:
        return False
    if not bars:
        return True
    last_ms = bars[-1].get("time") or 0
    return (now_ms - last_ms) > int(1.5 * interval_seconds * 1000)


async def backfill_active(delay: float = 0.2) -> int:
    """Force-refresh active strategies' (symbol, interval) specs via the provider + cache,
    bypassing the TTL. Best-effort: per-spec errors are logged and skipped."""
    provider = get_historical_provider()
    refreshed = 0
    async with session_scope() as s:
        rows = (await s.execute(select(StrategyState).where(
            StrategyState.status.in_(("scanning", "active"))))).scalars().all()
        active = [(r.key, r.selected_symbol) for r in rows]
    for key, selected in active:
        strat = get_strategy(key)
        if strat is None:
            continue
        for symbol, interval in strat.backfill_specs(selected):
            try:
                bars = await provider.get_bars(symbol, interval)
                history_cache.save(symbol, interval, bars, source=provider.name)
                refreshed += 1
            except Exception as exc:  # noqa: BLE001 — best-effort per spec
                logger.warning("backfill %s/%s failed: %r", symbol, interval, exc)
            if delay:
                await asyncio.sleep(delay)
    return refreshed
