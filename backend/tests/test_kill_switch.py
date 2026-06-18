"""Safety tests for the kill switch. Run: python -m tests.test_kill_switch"""

from __future__ import annotations

import asyncio

from app.engine.manager import EngineManager


async def _run() -> None:
    # 1. Cancels every tracked worker.
    mgr = EngineManager()
    started = asyncio.Event()

    async def worker():
        started.set()
        await asyncio.sleep(3600)  # would run "forever"

    await mgr.start_worker("s1", worker())
    await mgr.start_worker("s2", worker())
    await asyncio.wait_for(started.wait(), 1)
    assert set(mgr.running_workers()) == {"s1", "s2"}, mgr.running_workers()

    report = await mgr.kill_all()
    assert set(report.tasks_cancelled) == {"s1", "s2"}, report.tasks_cancelled
    assert mgr.running_workers() == [], mgr.running_workers()
    assert report.fully_flat is True
    print("PASS: cancels all workers")

    # 2. Idempotent — second press on empty registry is clean.
    report2 = await mgr.kill_all()
    assert report2.killed and report2.tasks_cancelled == [] and report2.fully_flat
    print("PASS: idempotent on empty registry")

    # 3. Flatten error is reported, NOT swallowed, and tasks still cancel.
    mgr2 = EngineManager()
    await mgr2.start_worker("s1", worker())

    async def bad_flatten() -> str:
        raise RuntimeError("broker down")

    report3 = await mgr2.kill_all(flatten=bad_flatten)
    assert report3.tasks_cancelled == ["s1"], report3.tasks_cancelled  # cancelled despite flatten fail
    assert report3.flatten_attempted and not report3.flatten_ok
    assert "broker down" in (report3.flatten_error or "")
    assert report3.fully_flat is False  # honest: not actually flat
    print("PASS: flatten failure reported, workers still cancelled, fully_flat=False")

    # 4. Successful flatten => fully_flat True.
    mgr3 = EngineManager()
    await mgr3.start_worker("s1", worker())

    async def ok_flatten() -> str:
        return "no open positions"

    report4 = await mgr3.kill_all(flatten=ok_flatten)
    assert report4.fully_flat and report4.flatten_ok
    print("PASS: successful flatten => fully_flat")

    print("\nALL KILL-SWITCH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
