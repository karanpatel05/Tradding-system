"""Local historical-candle cache.

After the first authenticated pull, candle history (daily + 5-min, per symbol) is
written to a local JSON file under ``history_cache_dir``. Reopening the strategy
page then reads bars from disk instead of re-hitting the API — faster, and it works
across restarts. A cache entry older than ``history_cache_ttl_hours`` triggers a
refetch; if that refetch fails (feed/auth hiccup) the **stale cache is reused** so
the page still renders.

Security/footprint: candle history is public market data (not secrets); the dir is
git-ignored. Files are plain JSON: ``{"fetched_at": iso, "interval": ..., "bars": [...]}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("trading.history_cache")

Fetcher = Callable[[], Awaitable[list[dict]]]

# Per-(source,symbol,interval) locks so two concurrent scans for the same key
# don't both miss the cache and both hit the provider (rate-limit friendly). The
# lock spans the cache check AND the fetch — see ``get_or_fetch``.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


def _dir() -> Path:
    return Path(get_settings().history_cache_dir)


def _path(symbol: str, interval: str, source: str = "") -> Path:
    # Namespace by data source so bars from a different provider never shadow
    # each other (e.g. legacy bars vs IBKR).
    stem = f"{source}__{symbol}__{interval}" if source else f"{symbol}__{interval}"
    return _dir() / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', stem)}.json"


def load(symbol: str, interval: str, source: str = "") -> dict | None:
    """Return the cached entry ``{"fetched_at", "bars"}`` or None."""
    path = _path(symbol, interval, source)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Unreadable cache %s: %s", path, exc)
        return None


def save(symbol: str, interval: str, bars: list[dict], source: str = "") -> None:
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        payload = {"symbol": symbol, "interval": interval, "source": source,
                   "fetched_at": datetime.now(timezone.utc).isoformat(), "bars": bars}
        _path(symbol, interval, source).write_text(json.dumps(payload))
    except OSError as exc:
        logger.warning("Could not write history cache for %s/%s: %s", symbol, interval, exc)


def _age_hours(fetched_at: str | None) -> float:
    if not fetched_at:
        return float("inf")
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


async def get_or_fetch(symbol: str, interval: str, fetcher: Fetcher,
                       source: str = "") -> list[dict]:
    """Return cached bars when fresh; otherwise fetch, persist, and return.

    ``source`` namespaces the cache by data provider. The whole check+fetch runs
    under a per-key lock so concurrent callers for the same key don't duplicate
    the provider request. On a fetch failure, fall back to whatever is cached
    (even if stale) so the page keeps working offline / through feed blips."""
    settings = get_settings()
    if not settings.history_cache_enabled:
        return await fetcher()

    async with _lock_for(f"{source}|{symbol}|{interval}"):
        cached = load(symbol, interval, source)
        if cached and _age_hours(cached.get("fetched_at")) < settings.history_cache_ttl_hours:
            return cached.get("bars", [])

        try:
            bars = await fetcher()
        except Exception as exc:  # noqa: BLE001 — fall back to stale cache if we have one
            if cached:
                logger.warning("Fetch failed for %s/%s (%s); using stale cache.", symbol, interval, exc)
                return cached.get("bars", [])
            raise

        # Only overwrite the cache with a non-empty pull; keep stale data otherwise.
        if bars:
            save(symbol, interval, bars, source)
            return bars
        return cached.get("bars", []) if cached else []


def clear() -> None:
    """Remove all cached history (e.g. on logout if desired)."""
    d = _dir()
    if not d.exists():
        return
    for f in d.glob("*.json"):
        try:
            f.unlink()
        except OSError:
            pass
