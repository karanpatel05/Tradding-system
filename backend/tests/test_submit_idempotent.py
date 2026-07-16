"""Live equity submit records a durable intent + sets the perm-id before/after transmit.
   Run: DATABASE_URL=sqlite+aiosqlite:///./_t_idem.db ENABLE_LIVE_ORDERS=1 python -m tests.test_submit_idempotent
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace


def _run() -> None:
    async def _t():
        from app.broker import orders as ord_mod
        from app.config import get_settings
        from app.db.base import init_db
        from app.engine.order_store import open_refs

        get_settings().enable_live_orders = True
        await init_db()

        captured = {}

        class Recorder:
            async def place_order(self, session, order, dry_run=True):
                return SimpleNamespace(order=SimpleNamespace(id=555))

        async def sess():
            return object()
        async def acct():
            return Recorder()
        async def mid(symbol):
            return 101.25

        from tastytrade.order import InstrumentType, Leg

        def _leg(sym, qty, action):
            return Leg(instrument_type=InstrumentType.EQUITY, symbol=sym, action=action,
                      quantity=Decimal(qty))

        async def fake_equity_get(session, symbols):
            sym = list(symbols)[0]
            return [SimpleNamespace(symbol=sym,
                                    build_leg=lambda qty, action: _leg(sym, qty, action))]

        ord_mod._session = sess
        ord_mod._account = acct
        ord_mod._mid = mid
        ord_mod._equities = fake_equity_get

        res = await ord_mod.submit_equity_order("AAPL", "enter_long", 10, reason="entry")
        assert res["transmitted"] is True, res
        assert res["ref"] in (await open_refs()), res
        captured["order_id"] = res["order_id"]
        assert captured["order_id"] == 555, captured
        print("PASS: live submit recorded a durable intent + set the perm-id")

    asyncio.run(_t())
    print("\nALL SUBMIT-IDEMPOTENT TESTS PASSED")


if __name__ == "__main__":
    _run()
