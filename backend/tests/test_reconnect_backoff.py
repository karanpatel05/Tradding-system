"""Backoff schedule is bounded/exponential; reconnect loop retries then connects.
   Run: python -m tests.test_reconnect_backoff
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    from app.ibkr.gateway import Gateway

    gw = Gateway()
    assert gw.backoff_schedule(0) == 1.0, gw.backoff_schedule(0)
    assert gw.backoff_schedule(3) == 8.0
    assert gw.backoff_schedule(10) == 60.0          # capped
    print("PASS: backoff schedule bounded + exponential")

    async def _loop_test():
        calls = {"n": 0}
        async def fake_connect():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("nope")
            return object()
        gw.connect = fake_connect          # type: ignore[assignment]
        gw.backoff_schedule = lambda a: 0.0  # type: ignore[assignment]  # no real waiting
        await gw.reconnect_loop(max_attempts=5)
        assert calls["n"] == 3, calls
    asyncio.run(_loop_test())
    print("PASS: reconnect loop retries until connected")

    print("\nALL RECONNECT TESTS PASSED")


if __name__ == "__main__":
    _run()
