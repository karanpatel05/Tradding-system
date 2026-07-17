"""Broker readiness = the Tastytrade OAuth pair verifies (token refresh probe).
Single source of truth for the auth gate, /status, the tt card, and strategy pause
conditions. Cached 5 min; a background refresher (main.py lifespan) keeps the sync
`is_ready_cached()` view fresh for non-async call sites."""
from __future__ import annotations

import time

_cache: dict = {"result": None, "at": 0.0}
_TTL = 300.0


async def _do_probe(force: bool) -> str | None:
    from app.config import get_settings
    s = get_settings()
    if not (s.tt_provider_secret and s.tt_refresh_token):
        return None
    try:
        from app.tastytrade.session import get_tt_session
        sess = await get_tt_session()
        if sess is None:
            return None
        await sess.refresh(force=True)
        return "ok"
    except Exception as e:  # noqa: BLE001 — surfaced, not raised
        return f"rejected: {str(e)[:200]}"


async def probe(force: bool = False) -> str | None:
    if not force and _cache["result"] is not None \
            and time.monotonic() - _cache["at"] < _TTL:
        return _cache["result"]
    result = await _do_probe(force)
    _cache.update(result=result, at=time.monotonic())
    return result


async def is_ready() -> bool:
    return (await probe()) == "ok"


def is_ready_cached() -> bool:
    return _cache["result"] == "ok"


async def refresher() -> None:
    """Lifespan task: keep the cached probe fresh (every 60s)."""
    import asyncio
    while True:
        try:
            await probe()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(60)
