"""Strategy engine: per-strategy async workers + the kill-switch task registry."""

from app.engine.manager import engine_manager

__all__ = ["engine_manager"]
