"""Broker-sourced snapshot + gateway-free recovery: mismatch trips SAFE_MODE; clean run
journals READY. reconcile() itself is unchanged and already covered."""
from types import SimpleNamespace

import pytest

from app.broker.account import NormalizedPosition
from app.engine import recovery as rec_mod
from app.engine import reconciliation as recon_mod
from app.engine.safe_mode import clear_safe_mode, is_safe_mode


def _pos(sym, qty):
    return NormalizedPosition(symbol=sym, quantity=qty, quantity_direction="Long",
                              instrument_type="Equity", avg_price=1.0, account="X")


@pytest.mark.asyncio
async def test_fetch_snapshot_from_broker(monkeypatch):
    async def poss():
        return [_pos("AAPL", 10)]
    async def orders():
        return [SimpleNamespace(id=5, status="Live",
                                legs=[SimpleNamespace(symbol="AAPL")])]
    monkeypatch.setattr(recon_mod, "_positions", poss)
    monkeypatch.setattr(recon_mod, "_live_orders", orders)
    snap = await recon_mod.fetch_snapshot()
    assert snap["positions"][0].symbol == "AAPL" and snap["positions"][0].qty == 10
    assert snap["orders"][0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_recovery_mismatch_trips_safe_mode(monkeypatch):
    from app.db.base import init_db
    await init_db()
    await clear_safe_mode()
    async def snap():
        return {"positions": [recon_mod.BrokerPosition("AAPL", 10, 1.0)],
                "fills": [], "orders": []}
    monkeypatch.setattr(rec_mod, "fetch_snapshot", snap)
    async def local():
        return {}                       # broker holds AAPL, we think we're flat
    monkeypatch.setattr(rec_mod, "local_open_positions", local)
    sent = []
    async def alert(kind, reason, meta=None):
        sent.append(kind)
    monkeypatch.setattr("app.services.alerts.send_alert", alert)
    ok = await rec_mod.run_recovery()
    assert ok is False and await is_safe_mode() is True and "safe_mode" in sent
    await clear_safe_mode()


@pytest.mark.asyncio
async def test_recovery_clean(monkeypatch):
    from app.db.base import init_db
    await init_db()
    await clear_safe_mode()
    async def snap():
        return {"positions": [], "fills": [], "orders": []}
    monkeypatch.setattr(rec_mod, "fetch_snapshot", snap)
    async def local():
        return {}
    monkeypatch.setattr(rec_mod, "local_open_positions", local)
    assert await rec_mod.run_recovery() is True
    assert await is_safe_mode() is False
