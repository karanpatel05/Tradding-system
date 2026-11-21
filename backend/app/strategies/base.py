"""Base strategy contract.

A concrete strategy fills in ``ready = True`` plus the overview/criteria metadata
and the ``scan``/``evaluate`` logic. Until then it stays "Coming Soon" and the
engine/API treat it as not tradable.

The engine only ever talks to this interface, so dropping in real logic later
requires no engine changes (the whole point of the scaffold).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Candidate:
    """A ranked stock candidate produced by a scan."""

    symbol: str
    score: float
    reason: str = ""
    metrics: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    #: Stable key, e.g. "strategy_1".
    key: str = ""
    #: Human-readable name.
    name: str = ""
    #: Flip to True once real rules exist. Gates activation/allocation everywhere.
    ready: bool = False
    #: True when the strategy executes outside this app (shown, never run here).
    external_runner: bool = False
    #: Set when the app's equity backtester does not apply to this strategy; the
    #: backtest routes then return this note instead of running it.
    backtest_note: str | None = None

    # --- metadata (shown in the Strategy tab once ready) ------------------
    description: str = "Coming Soon"
    rules: list[str] = []
    entry_logic: str = ""
    exit_logic: str = ""
    selection_criteria: str = ""

    def overview(self) -> dict:
        """Strategy-tab overview. Returns a Coming-Soon shell when not ready."""
        if not self.ready:
            return {"key": self.key, "name": self.name, "ready": False,
                    "status": "Coming Soon"}
        return {
            "key": self.key,
            "name": self.name,
            "ready": True,
            "description": self.description,
            "rules": self.rules,
            "entry_logic": self.entry_logic,
            "exit_logic": self.exit_logic,
            "selection_criteria": self.selection_criteria,
        }

    def activation_block_reason(self) -> str | None:
        """Why this (ready) strategy must not run in this app right now, or None.
        Non-None refuses activation (409) and skips auto-resume at boot."""
        return None

    # --- engine hooks (implemented by ready strategies) -------------------
    @abstractmethod
    async def scan(self, universe: list[str]) -> list[Candidate]:
        """Rank the universe and return scored candidates (best first)."""
        raise NotImplementedError

    async def select_best(self, universe: list[str]) -> Candidate | None:
        """Pick the single best-fit stock for this strategy, or None."""
        candidates = await self.scan(universe)
        return candidates[0] if candidates else None

    # --- resilience hooks (data gap gate + proactive backfill) ------------
    def bar_interval(self) -> str:
        """Candle interval the live loop acts on (for the data gap gate)."""
        return "5m"

    def backfill_specs(self, selected_symbol: str | None) -> list[tuple[str, str]]:
        """(symbol, interval) pairs to force-refresh after a reconnect. Default: none."""
        return []


class ComingSoonStrategy(BaseStrategy):
    """Placeholder used for the 5 not-yet-implemented strategies. Its scan raises,
    which is fine because the engine refuses to run a not-ready strategy."""

    ready = False

    def __init__(self, key: str, name: str) -> None:
        self.key = key
        self.name = name

    async def scan(self, universe: list[str]):  # pragma: no cover - never called
        raise NotImplementedError(f"{self.key} has no logic yet (Coming Soon).")
