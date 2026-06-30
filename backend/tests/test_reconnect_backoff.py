"""Reconnect loop retries at a fixed interval until connected.
   Run: python -m tests.test_reconnect_backoff
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.config import get_settings
    from app.ibkr.gateway import Gateway

    assert not hasattr(Gateway, "backoff_schedule"), "exponential backoff_schedule should be gone"

    async def _loop_test():
        get_settings().ibkr_reconnect_interval_seconds = 0   # no real waiting in the test
        gw = Gateway()
        calls = {"n": 0}
        async def fake_connect():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("nope")
            return object()
        gw.connect = fake_connect          # type: ignore[assignment]
        await gw.reconnect_loop(max_attempts=5)
        assert calls["n"] == 3, calls
    asyncio.run(_loop_test())
    print("PASS: reconnect loop retries at fixed interval until connected")
    print("\nALL RECONNECT TESTS PASSED")


if __name__ == "__main__":
    _run()
