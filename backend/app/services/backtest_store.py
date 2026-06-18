"""Local persistence for the most recent backtest result per strategy.

A completed backtest is written to a JSON file under ``history_cache_dir`` so it
reloads automatically when the strategy page is reopened or the server restarts —
the user doesn't have to re-run it. Backtest output is derived from public market
data (no secrets); the dir is git-ignored.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("trading.backtest_store")


def _path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return Path(get_settings().history_cache_dir) / f"backtest_{safe}.json"


def save(key: str, result: dict) -> None:
    try:
        path = _path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result))
    except OSError as exc:
        logger.warning("Could not persist backtest result for %s: %s", key, exc)


def load(key: str) -> dict | None:
    path = _path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Unreadable backtest result for %s: %s", key, exc)
        return None
