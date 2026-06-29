"""Guard: LivePriceSource.achain takes only (self, underlying) — both callers
(strategy_class + smoke) call it with one arg. A live regression here crashes the
live TC path (no unit test hits it because it needs a Gateway), so check the signature.
   Run: python -m tests.test_tc_live_source_sig
"""
from __future__ import annotations

import inspect


def _run() -> None:
    from app.strategies.triple_calendar.live_source import LivePriceSource

    params = list(inspect.signature(LivePriceSource.achain).parameters)
    assert params == ["self", "underlying"], params
    print("PASS: achain(self, underlying) signature")
    print("\nALL TC-LIVE-SOURCE-SIG TESTS PASSED")


if __name__ == "__main__":
    _run()
