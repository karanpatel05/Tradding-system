"""Strategy registry: the 5 strategies and DB-state seeding.

To add real logic later: implement a ``BaseStrategy`` subclass with ``ready=True``
and replace the corresponding ``ComingSoonStrategy`` entry here. Nothing else in
the engine or API changes.
"""

from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import StrategyState
from app.strategies.base import BaseStrategy, ComingSoonStrategy
from app.strategies.qqq_credit_spread import QqqCreditSpreadStrategy

settings = get_settings()

# The QQQ first-hour credit spread is listed with its real rules but runs OUTSIDE this
# app (com.karan.spreadlive); it is switched off here (activation always refused).
# The other slots are "Coming Soon" placeholders. The 9/15 EMA crossover (slot
# strategy_1) and the Triple Calendar (slot strategy_2) were removed. To make a slot
# live, register a ready BaseStrategy subclass under its key.
_qqq = QqqCreditSpreadStrategy()
_STRATEGIES: dict[str, BaseStrategy] = {
    _qqq.key: _qqq,
    **{
        f"strategy_{i}": ComingSoonStrategy(key=f"strategy_{i}", name=f"Strategy {i}")
        for i in range(2, settings.max_strategies + 1)
    },
}


def list_strategies() -> list[BaseStrategy]:
    return list(_STRATEGIES.values())


def get_strategy(key: str) -> BaseStrategy | None:
    return _STRATEGIES.get(key)


async def seed_strategy_state() -> None:
    """Ensure a StrategyState row exists for each registered strategy. Keeps the
    DB ``ready`` flag in sync with the code (source of truth = the strategy class)."""
    async with session_scope() as session:
        existing = {
            row.key
            for row in (await session.execute(select(StrategyState))).scalars().all()
        }
        for strat in _STRATEGIES.values():
            if strat.key not in existing:
                session.add(StrategyState(
                    key=strat.key, name=strat.name, ready=strat.ready, status="idle",
                ))
            else:
                # Sync the ready flag + name in case code changed (e.g. a strategy
                # was removed and its slot reverted to a Coming Soon placeholder).
                state = await session.get(StrategyState, strat.key)
                if state and (state.ready != strat.ready or state.name != strat.name):
                    state.ready = strat.ready
                    state.name = strat.name
                if state and not state.ready and state.status != "idle":
                    state.status = "idle"
                # A blocked strategy (e.g. one that runs outside this app) must never
                # come back up as active from a persisted status.
                if (state and state.status in ("scanning", "active", "paused")
                        and strat.activation_block_reason()):
                    state.status = "idle"
