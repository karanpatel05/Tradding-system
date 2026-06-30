"""get_account_summary must not open multiple IBKR account-summary subscriptions when
called concurrently with a cold cache (IBKR error 322). Concurrent callers should share
one subscription.
   Run: python -m tests.test_account_summary_single_sub
"""
from __future__ import annotations

import asyncio


def _run() -> None:
    async def _t():
        from app.ibkr.account import get_account_summary
        from app.ibkr.gateway import get_gateway

        class Row:
            def __init__(self, tag, value, account="DU1", currency="USD"):
                self.tag, self.value, self.account, self.currency = tag, value, account, currency

        class StubIB:
            """Mimics ib_async: opens a subscription only when its cache is cold, with a
            small latency before the data populates the cache."""
            def __init__(self):
                self.subs = 0
                self._cache = None
            async def accountSummaryAsync(self, account=""):
                if self._cache is None:
                    self.subs += 1
                    await asyncio.sleep(0.05)   # data latency — the race window
                    self._cache = [Row("NetLiquidation", "1000")]
                return self._cache

        gw = get_gateway()
        gw._ib = StubIB()
        gw.is_connected = lambda: True   # type: ignore[assignment]

        # Two concurrent callers (dashboard + WS snapshot) hit a cold cache at once.
        a, b = await asyncio.gather(get_account_summary(), get_account_summary())
        assert gw._ib.subs == 1, f"opened {gw._ib.subs} subscriptions (should be 1) — error 322 risk"
        assert a["net_liquidation"] == 1000.0 and b["net_liquidation"] == 1000.0, (a, b)
        print("PASS: concurrent get_account_summary shares one subscription")
    asyncio.run(_t())
    print("\nALL ACCOUNT-SUMMARY-SUB TESTS PASSED")


if __name__ == "__main__":
    _run()
