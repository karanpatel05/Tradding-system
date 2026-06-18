"""Modular strategy framework. Strategies 1–5 are scaffolded as "Coming Soon";
real logic drops into a subclass without touching the engine."""

from app.strategies.registry import get_strategy, list_strategies

__all__ = ["get_strategy", "list_strategies"]
