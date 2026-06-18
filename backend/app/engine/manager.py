"""Engine task registry + kill switch.

This is the safety-critical spine of the system. Every strategy runs as an
isolated :class:`asyncio.Task` registered here so it can be started, stopped, and
— above all — *killed* cleanly.

Kill-switch guarantees (these hold even though trade execution is still stubbed):

* **Cancels every tracked task**, even if position-flattening raises.
* **Idempotent** — a second press (or a press with nothing running) returns a
  clean report rather than erroring.
* **Reports partial failure** — the caller learns exactly which tasks would not
  cancel and whether flatten succeeded, instead of a silently-swallowed error.

"Zero exposure" is treated as a guarantee, not best-effort: workers are cancelled
*first* (so no new orders can be placed) and only then are positions flattened.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("trading.engine")

# A flatten function takes nothing and returns a human-readable detail string.
FlattenFn = Callable[[], Awaitable[str]]


@dataclass
class KillReport:
    killed: bool
    started_at: datetime
    tasks_cancelled: list[str] = field(default_factory=list)
    tasks_failed: dict[str, str] = field(default_factory=dict)
    flatten_attempted: bool = False
    flatten_ok: bool = False
    flatten_detail: str = ""
    flatten_error: str | None = None

    @property
    def fully_flat(self) -> bool:
        """True only if every task cancelled AND flatten succeeded."""
        return not self.tasks_failed and (not self.flatten_attempted or self.flatten_ok)

    def to_dict(self) -> dict:
        return {
            "killed": self.killed,
            "started_at": self.started_at.isoformat(),
            "tasks_cancelled": self.tasks_cancelled,
            "tasks_failed": self.tasks_failed,
            "flatten": {
                "attempted": self.flatten_attempted,
                "ok": self.flatten_ok,
                "detail": self.flatten_detail,
                "error": self.flatten_error,
            },
            "fully_flat": self.fully_flat,
        }


class EngineManager:
    """Process-wide registry of strategy worker tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._kill_lock = asyncio.Lock()

    # --- worker lifecycle -------------------------------------------------
    async def start_worker(self, worker_id: str, coro: Awaitable) -> None:
        """Register and launch a worker coroutine as a tracked task.

        Replaces any existing task for the same id (cancelling the old one first).
        """
        async with self._lock:
            existing = self._tasks.get(worker_id)
            if existing and not existing.done():
                existing.cancel()
            task = asyncio.ensure_future(coro)
            self._tasks[worker_id] = task
            task.add_done_callback(lambda t, wid=worker_id: self._on_done(wid, t))
            logger.info("Started worker %s", worker_id)

    def _on_done(self, worker_id: str, task: asyncio.Task) -> None:
        # Surface crashes; don't leave a dead task in the registry.
        if task.cancelled():
            logger.info("Worker %s cancelled", worker_id)
        elif (exc := task.exception()) is not None:
            logger.error("Worker %s crashed: %r", worker_id, exc)
        # Only drop it if it's still the registered task (not replaced).
        if self._tasks.get(worker_id) is task:
            self._tasks.pop(worker_id, None)

    async def stop_worker(self, worker_id: str) -> bool:
        """Cancel a single worker. Returns True if one was running."""
        async with self._lock:
            task = self._tasks.get(worker_id)
            if not task or task.done():
                return False
            task.cancel()
        await self._await_cancelled(task)
        return True

    def running_workers(self) -> list[str]:
        return [wid for wid, t in self._tasks.items() if not t.done()]

    def is_running(self, worker_id: str) -> bool:
        task = self._tasks.get(worker_id)
        return bool(task and not task.done())

    # --- kill switch ------------------------------------------------------
    async def kill_all(self, flatten: FlattenFn | None = None) -> KillReport:
        """Cancel every tracked task, then flatten positions. Idempotent.

        Cancellation is attempted for *all* tasks regardless of individual
        failures, and flatten runs even if some task failed to cancel.
        """
        report = KillReport(killed=True, started_at=datetime.now(timezone.utc))

        # Serialize concurrent kill presses; a second press still produces a
        # valid (clean) report because the registry is already empty.
        async with self._kill_lock:
            # Snapshot then clear so re-entry is safe.
            async with self._lock:
                tasks = dict(self._tasks)
                self._tasks.clear()

            for wid, task in tasks.items():
                if not task.done():
                    task.cancel()

            for wid, task in tasks.items():
                try:
                    await self._await_cancelled(task)
                    report.tasks_cancelled.append(wid)
                except Exception as exc:  # noqa: BLE001 — must not abort the loop
                    report.tasks_failed[wid] = repr(exc)
                    logger.error("Failed to cancel worker %s: %r", wid, exc)

            # Flatten runs regardless of task-cancel outcomes.
            if flatten is not None:
                report.flatten_attempted = True
                try:
                    report.flatten_detail = await flatten()
                    report.flatten_ok = True
                except Exception as exc:  # noqa: BLE001
                    report.flatten_error = repr(exc)
                    logger.error("Flatten failed during kill: %r", exc)

        logger.warning("KILL SWITCH: %s", report.to_dict())
        return report

    @staticmethod
    async def _await_cancelled(task: asyncio.Task) -> None:
        """Await a cancelled task, swallowing the expected CancelledError but
        re-raising anything else so the caller can record a real failure."""
        try:
            await task
        except asyncio.CancelledError:
            pass


# Module-level singleton.
engine_manager = EngineManager()
