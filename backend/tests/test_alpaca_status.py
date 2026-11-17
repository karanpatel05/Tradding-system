"""GET /api/auth/alpaca: read-only Alpaca status — never echoes keys, masks the account,
maps 401 to 'rejected', and reports not_configured without calling Alpaca."""
import httpx
import pytest

from app.auth import routes as auth_routes
from app.config import get_settings
from app.services import alpaca_status as svc


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(svc.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    svc._cache.update(at=0.0, value=None)
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "PKTESTKEY")
    monkeypatch.setattr(s, "apca_api_secret_key", "topsecret")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")


@pytest.mark.asyncio
async def test_ok_masks_and_hides_secrets(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("APCA-API-KEY-ID")
        return httpx.Response(200, json={"account_number": "PA12345678", "status": "ACTIVE",
                                         "currency": "USD", "equity": "100000.5",
                                         "cash": "90000", "buying_power": "200000"})
    _patch_transport(monkeypatch, handler)

    out = await auth_routes.alpaca_status(probe=True)
    assert out["status"] == "ok" and out["paper"] is True
    assert out["account"] == "••••5678" and out["equity"] == 100000.5
    assert seen["url"].endswith("/v2/account") and seen["key"] == "PKTESTKEY"
    assert "topsecret" not in str(out) and "PKTESTKEY" not in str(out)


@pytest.mark.asyncio
async def test_rejected_keys(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(401, json={"message": "unauthorized"}))
    out = await auth_routes.alpaca_status(probe=True)
    assert out["status"] == "rejected"


@pytest.mark.asyncio
async def test_not_configured_skips_network(monkeypatch):
    monkeypatch.setattr(get_settings(), "apca_api_key_id", "")

    def boom(req):
        raise AssertionError("must not call Alpaca when unconfigured")
    _patch_transport(monkeypatch, boom)
    out = await auth_routes.alpaca_status()
    assert out == {"configured": False, "paper": True, "status": "not_configured"}


@pytest.mark.asyncio
async def test_cached_between_polls(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"account_number": "PA1", "status": "ACTIVE"})
    _patch_transport(monkeypatch, handler)
    await auth_routes.alpaca_status()
    await auth_routes.alpaca_status()
    assert len(calls) == 1
