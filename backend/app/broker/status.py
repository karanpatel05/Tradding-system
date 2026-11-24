"""Broker readiness = the Tastytrade OAuth pair verifies (token refresh probe).
Single source of truth for the auth gate, /status, the tt card, and strategy pause
conditions. A background refresher (main.py lifespan) keeps the sync
`is_ready_cached()` view fresh for non-async call sites.

Probe behaviour (stale-while-revalidate, single-flight):

* ``probe()`` with any cached result returns it immediately — even when it is older
  than ``_TTL`` — and, if stale, schedules at most one background refresh. Hot paths
  (the ``/api/auth/status`` badge poll, ``is_ready()``) therefore never wait on a live
  Tastytrade token refresh.
* Only a cold call (nothing cached yet, ``_cache["at"] == 0``) awaits, and it waits at
  most ``_WAIT_TIMEOUT`` seconds; past that it caches and returns ``"rejected: timeout"``
  (always treated as stale, so the next call schedules a retry) while the refresh keeps
  running in the background and overwrites the cache when it lands.
* ``probe(force=True)`` (connect / creds save / resume) runs a real fresh probe,
  bounded by the same ``_WAIT_TIMEOUT``. Concurrent forced calls share one probe; a
  forced call supersedes an in-flight *background* refresh (which may hold a session
  built from older creds), and that older refresh can no longer overwrite the newer
  result. A forced timeout returns ``"rejected: timeout"`` without clobbering an
  existing cached result.
* Every refresh is itself bounded by ``_PROBE_TIMEOUT`` so a hung request can never
  wedge the single-flight slot.
"""
from __future__ import annotations

import asyncio
import time

_cache: dict = {"result": None, "at": 0.0}
_TTL = 300.0
_WAIT_TIMEOUT = 5.0      # longest any caller waits on a live refresh
_PROBE_TIMEOUT = 20.0    # hard bound on a single background refresh
TIMEOUT_RESULT = "rejected: timeout"

_inflight: asyncio.Task | None = None   # the refresh new callers join
_tasks: set[asyncio.Task] = set()       # strong refs (incl. superseded refreshes):
                                        # asyncio only holds tasks weakly
_inflight_forced = False
_gen = 0            # generation handed to each new refresh
_written_gen = 0    # generation of the last refresh that wrote the cache


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


async def _run(gen: int, force: bool) -> str | None:
    global _written_gen
    try:
        result = await asyncio.wait_for(_do_probe(force), _PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        result = TIMEOUT_RESULT
    if gen >= _written_gen:   # a newer (forced) probe already wrote: don't clobber it
        _written_gen = gen
        _cache.update(result=result, at=time.monotonic())
    return result


def _ensure_task(force: bool) -> asyncio.Task:
    """Return the shared in-flight refresh, starting one if needed (single-flight)."""
    global _inflight, _inflight_forced, _gen
    loop = asyncio.get_running_loop()
    task = _inflight
    if task is not None and not task.done() and task.get_loop() is loop \
            and (_inflight_forced or not force):
        return task
    _gen += 1
    task = loop.create_task(_run(_gen, force), name="tt-probe")
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    _inflight, _inflight_forced = task, force
    return task


def _is_stale() -> bool:
    return _cache["result"] == TIMEOUT_RESULT \
        or time.monotonic() - _cache["at"] >= _TTL


async def probe(force: bool = False) -> str | None:
    cold = _cache["at"] == 0.0
    if not force and not cold:
        if _is_stale():
            _ensure_task(False)
        return _cache["result"]
    task = _ensure_task(force)
    try:
        return await asyncio.wait_for(asyncio.shield(task), _WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        if _cache["at"] == 0.0:   # still cold: cache the timeout so polls stop waiting
            _cache.update(result=TIMEOUT_RESULT, at=time.monotonic())
        return TIMEOUT_RESULT


async def is_ready() -> bool:
    return (await probe()) == "ok"


def is_ready_cached() -> bool:
    return _cache["result"] == "ok"


async def refresher() -> None:
    """Lifespan task: keep the cached probe fresh (every 60s)."""
    while True:
        try:
            await probe()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(60)
