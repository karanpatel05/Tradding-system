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


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://paper-api.alpaca.markets.evil.com",
                                 "http://paper-api.alpaca.markets",
                                 "https://evil.com@paper-api.alpaca.markets",
                                 "https://api.alpaca.markets"])
async def test_paper_flag_uses_the_exact_host_lock_rule(monkeypatch, url):
    """`paper` must agree with the write lock (no substring match)."""
    monkeypatch.setattr(get_settings(), "apca_api_base_url", url)
    monkeypatch.setattr(get_settings(), "apca_api_key_id", "")
    out = await svc.fetch_alpaca_status()
    assert out["paper"] is False


@pytest.mark.asyncio
async def test_auth_status_does_not_fetch_alpaca_balances(monkeypatch):
    """The Tastytrade connection badge polls /api/auth/status every few seconds: it must
    not call Alpaca /v2/account nor show the Alpaca account under market data."""
    async def ok_probe(force=False):
        return "ok"

    called = []

    async def no_balances():
        called.append(1)                    # (a raise here would be swallowed)
        return {"account": "PA12345678"}

    async def no_sm():
        return False, None
    monkeypatch.setattr(auth_routes, "probe", ok_probe)
    monkeypatch.setattr(auth_routes, "_safe_mode_info", no_sm)
    monkeypatch.setattr("app.broker.account.balances", no_balances)
    _patch_transport(monkeypatch, lambda req: (_ for _ in ()).throw(
        AssertionError("network touched")))
    out = await auth_routes._status()
    assert called == [], "auth status must not read Alpaca balances"
    assert out.account == "" and out.accounts == []
