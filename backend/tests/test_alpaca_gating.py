"""Dashboard/WS account views gate on Alpaca readiness (cached), not the Tastytrade probe."""
import pytest
from fastapi.testclient import TestClient

from app.broker import alpaca_client
from app.broker import status as tt_status
from app.config import get_settings
from app.main import app
from app.routers import dashboard, ws
from app.services import alpaca_status as svc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_auth_token", "tok")
    monkeypatch.setattr(s, "apca_api_key_id", "PKTEST")
    monkeypatch.setattr(s, "apca_api_secret_key", "sec")
    # Tastytrade probe is NOT ok — must not matter any more.
    monkeypatch.setattr(tt_status, "_cache", {"result": "rejected", "at": 1e9})
    svc._cache.update(at=0.0, value=None)
    yield
    svc._cache.update(at=0.0, value=None)


def _client():
    return TestClient(app)  # no lifespan: no startup side effects


def test_account_endpoint_uses_alpaca_readiness(monkeypatch):
    svc._cache.update(at=1.0, value={"status": "ok"})

    async def fake_balances():
        return {"net_liquidation": 123.0}
    monkeypatch.setattr(dashboard, "get_account_summary", fake_balances)
    r = _client().get("/api/account", headers={"X-App-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"account": {"net_liquidation": 123.0}, "note": None}


def test_account_endpoint_when_alpaca_down(monkeypatch):
    svc._cache.update(at=1.0, value={"status": "error"})

    async def boom():
        raise AssertionError("must not fetch when Alpaca is not ready")
    monkeypatch.setattr(dashboard, "get_account_summary", boom)
    r = _client().get("/api/account", headers={"X-App-Token": "tok"})
    assert r.json() == {"account": {}, "note": "Alpaca not connected."}


def test_positions_endpoint_when_alpaca_down():
    svc._cache.update(at=1.0, value=None)
    r = _client().get("/api/positions", headers={"X-App-Token": "tok"})
    assert r.json() == {"positions": [], "note": "Alpaca not connected."}


def test_ready_cached_requires_ok_and_configured(monkeypatch):
    assert svc.alpaca_ready_cached() is False
    svc._cache.update(at=1.0, value={"status": "ok"})
    assert svc.alpaca_ready_cached() is True
    monkeypatch.setattr(alpaca_client, "configured", lambda: False)
    assert svc.alpaca_ready_cached() is False


@pytest.mark.asyncio
async def test_ws_snapshots_gate_on_alpaca(monkeypatch):
    async def fake_balances():
        return {"net_liquidation": 5.0}
    monkeypatch.setattr(ws, "get_account_summary", fake_balances)
    assert await ws._account_snapshot() == {}
    svc._cache.update(at=1.0, value={"status": "ok"})
    assert await ws._account_snapshot() == {"net_liquidation": 5.0}


@pytest.mark.asyncio
async def test_alpaca_refresher_swallows_errors(monkeypatch):
    import asyncio
    calls = []

    async def flaky(force=False):
        calls.append(force)
        if len(calls) == 1:
            raise RuntimeError("x")
    monkeypatch.setattr(svc, "fetch_alpaca_status", flaky)

    async def fast_sleep(_):
        if len(calls) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(svc.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await svc.alpaca_refresher()
    assert calls == [True, True]
