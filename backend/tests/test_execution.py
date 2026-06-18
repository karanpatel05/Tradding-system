"""Exercises the *authenticated* positions/flatten data path with a mock SDK
shape — the branch no other test reaches (they all see the degraded/401 path).
This is what guards against the `Account(account_number=...)` class of bug, and
now also against a kill switch that falsely reports zero exposure.

Run: python -m tests.test_execution
"""

from __future__ import annotations

import asyncio


class FakeSession:
    async def refresh(self, force: bool = False):  # matches SDK no-op-when-fresh
        return None


class FakeAccount:
    """Mimics tastytrade.Account: has .account_number and .get_positions(session)."""

    def __init__(self, number, positions):
        self.account_number = number
        self._positions = positions

    async def get_positions(self, session):
        return self._positions


class FakeInstrumentType:
    def __init__(self, value):
        self.value = value


class FakePosition:
    def __init__(self, symbol, quantity, direction="Long", instrument="Equity"):
        self.symbol = symbol
        self.quantity = quantity
        self.quantity_direction = direction
        self.instrument_type = FakeInstrumentType(instrument)


async def _run() -> None:
    from app.auth.manager import session_manager
    from app.config import get_settings
    from app.engine import execution
    from app.engine.execution import flatten_all
    from app.tastytrade.client import get_client

    client = get_client()
    accounts = [FakeAccount("5WT0001", [
        FakePosition("AAPL", 10, direction="Long"),
        FakePosition("MSFT", 5, direction="Short"),
    ])]

    # Authenticate with a fake session and stub the account fetch.
    session_manager.establish(FakeSession())
    client.get_accounts = lambda: _wrap(accounts)  # type: ignore

    # get_positions must thread the Account object straight through (no reconstruct).
    pos = await client.get_positions(accounts[0])
    assert [p.symbol for p in pos] == ["AAPL", "MSFT"], pos
    print("PASS: get_positions threads the Account object")

    settings = get_settings()
    assert settings.enable_live_orders is False, "test assumes the default paper gate"

    # PAPER MODE: positions exist but nothing is transmitted, so flatten must NOT
    # claim success — it raises so the kill report stays honest (fully_flat=False).
    try:
        await flatten_all()
    except RuntimeError as exc:
        msg = str(exc)
        assert "AAPL" in msg and "MSFT" in msg and "paper" in msg.lower(), msg
        print("PASS: paper mode with open positions raises (not silently 'flat')")
    else:
        raise AssertionError("flatten_all should raise in paper mode with open positions")

    # LIVE MODE: real transmission path. Stub the broker call and assert each
    # position is closed with the correct direction-aware action.
    calls: list[tuple] = []

    async def fake_place(session, account, symbol, action, quantity):
        calls.append((symbol, action, quantity))
        return object()

    settings.enable_live_orders = True
    orig_place = execution._place_equity_order
    execution._place_equity_order = fake_place  # type: ignore
    try:
        detail = await flatten_all()
    finally:
        execution._place_equity_order = orig_place  # type: ignore
        settings.enable_live_orders = False

    assert ("AAPL", "Sell to Close", 10) in calls, calls  # long -> sell to close
    assert ("MSFT", "Buy to Close", 5) in calls, calls     # short -> buy to close, abs(qty)
    assert "2 position" in detail and "AAPL x10" in detail and "MSFT x5" in detail, detail
    print("PASS: live mode transmits correct closing orders (long/short)")

    # A position we can't auto-close (option) keeps the report honest even live.
    opt_accounts = [FakeAccount("5WT0001", [FakePosition("AAPL  240119C", 1, instrument="Equity Option")])]
    client.get_accounts = lambda: _wrap(opt_accounts)  # type: ignore
    settings.enable_live_orders = True
    execution._place_equity_order = fake_place  # type: ignore
    try:
        await flatten_all()
    except RuntimeError as exc:
        assert "not an equity" in str(exc), exc
        print("PASS: non-equity position reported as not-flat")
    else:
        raise AssertionError("non-equity position should raise")
    finally:
        execution._place_equity_order = orig_place  # type: ignore
        settings.enable_live_orders = False

    # Empty-positions case.
    client.get_accounts = lambda: _wrap([FakeAccount("5WT0001", [])])  # type: ignore
    assert "No open positions" in await flatten_all()
    print("PASS: flatten_all handles zero positions")

    session_manager.clear()
    assert "nothing to flatten" in (await flatten_all()).lower()
    print("PASS: flatten_all no-ops when unauthenticated")

    print("\nALL EXECUTION TESTS PASSED")


async def _wrap(value):
    return value


if __name__ == "__main__":
    asyncio.run(_run())
