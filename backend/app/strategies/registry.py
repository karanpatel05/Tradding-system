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
from app.strategies.ema_crossover import EmaCrossoverStrategy

settings = get_settings()

# Ordered registry of the 5 strategies. strategy_1 is the live multi-position 9/15
# EMA *crossover* (it replaced the old single-position scalper; the scalper package
# survives only for its shared indicators). The rest remain "Coming Soon" until
# their rules are implemented.
_STRATEGIES: dict[str, BaseStrategy] = {
    f"strategy_{i}": ComingSoonStrategy(key=f"strategy_{i}", name=f"Strategy {i}")
    for i in range(2, settings.max_strategies + 1)
}
_xover = EmaCrossoverStrategy()
_STRATEGIES = {_xover.key: _xover, **_STRATEGIES}


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
                # Sync the ready flag in case code changed.
                state = await session.get(StrategyState, strat.key)
                if state and state.ready != strat.ready:
                    state.ready = strat.ready
