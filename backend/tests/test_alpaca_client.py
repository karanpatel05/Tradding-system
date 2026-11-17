"""Alpaca client: paper lock, URL normalization, error mapping, ownership tag. No network."""
import httpx
import pytest

from app.broker import alpaca_client as ac
from app.config import get_settings


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "PKTESTKEY")
    monkeypatch.setattr(s, "apca_api_secret_key", "topsecret")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")


@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets/",
    "https://paper-api.alpaca.markets/v2",
    "https://paper-api.alpaca.markets/v2/",
])
def test_trading_base_normalizes_paper_url_variants(monkeypatch, url):
    monkeypatch.setattr(get_settings(), "apca_api_base_url", url)
    assert ac.trading_base() == "https://paper-api.alpaca.markets"


def test_trading_base_locks_live_host(monkeypatch):
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    with pytest.raises(ac.PaperLockError):
        ac.trading_base()
    assert ac.base_url() == "https://api.alpaca.markets"


@pytest.mark.asyncio
async def test_request_write_to_live_host_never_sends(monkeypatch):
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    calls = []
    _patch_transport(monkeypatch, lambda req: calls.append(req) or httpx.Response(200, json={}))
    with pytest.raises(ac.PaperLockError):
        await ac.request("POST", "/v2/orders", json={}, write=True)
    assert calls == []


@pytest.mark.asyncio
async def test_request_raises_alpaca_error_with_message(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(422, json={"message": "insufficient qty"}))
    with pytest.raises(ac.AlpacaError) as ei:
        await ac.request("POST", "/v2/orders", json={}, write=True)
    assert ei.value.status == 422 and ei.value.message == "insufficient qty"


@pytest.mark.asyncio
async def test_request_success_sends_headers_and_handles_204(monkeypatch):
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("APCA-API-KEY-ID")
        seen["secret"] = req.headers.get("APCA-API-SECRET-KEY")
        seen["url"] = str(req.url)
        return httpx.Response(204) if req.method == "DELETE" else httpx.Response(200, json={"ok": 1})
    _patch_transport(monkeypatch, handler)
    assert await ac.request("GET", "/v2/account", params={"a": "b"}) == {"ok": 1}
    assert seen["key"] == "PKTESTKEY" and seen["secret"] == "topsecret"
    assert seen["url"] == "https://paper-api.alpaca.markets/v2/account?a=b"
    assert await ac.request("DELETE", "/v2/orders/1", write=True) is None


def test_tag_and_ownership():
    t = ac.tag("x" * 60)
    assert len(t) == 48 and t.startswith("tsys-")
    assert ac.is_app_owned("tsys-a") is True
    assert ac.is_app_owned("abc") is False
    assert ac.is_app_owned(None) is False


def test_occ_unpadded():
    assert ac.occ_unpadded("QQQ   261117C00660000") == "QQQ261117C00660000"
    assert ac.occ_unpadded("AAPL") == "AAPL"


def test_configured(monkeypatch):
    assert ac.configured() is True
    monkeypatch.setattr(get_settings(), "apca_api_secret_key", " ")
    assert ac.configured() is False


# ---------------------------------------------------- M3: lock hardening
@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets.evil.com",          # suffix spoof
    "https://paper-api.alpaca.markets@evil.com",          # userinfo -> host is evil.com
    "https://evil.com@paper-api.alpaca.markets",          # userinfo on the paper host
    "http://paper-api.alpaca.markets",                    # plain http
    "https://paper-api.alpaca.markets:8443",              # non-standard port
    "https://api.alpaca.markets",
])
def test_trading_base_rejects_adversarial_urls(monkeypatch, url):
    monkeypatch.setattr(get_settings(), "apca_api_base_url", url)
    with pytest.raises(ac.PaperLockError):
        ac.trading_base()
    assert ac.is_paper_url(ac.base_url()) is False


def test_is_paper_url_accepts_only_exact_paper_host():
    assert ac.is_paper_url("https://paper-api.alpaca.markets") is True
    assert ac.is_paper_url("https://PAPER-API.alpaca.markets:443") is True
    assert ac.is_paper_url("http://paper-api.alpaca.markets") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["DELETE", "POST", "PATCH", "put"])
async def test_non_get_is_always_a_write(monkeypatch, method):
    """A non-GET without write=True must still be paper-locked (never sent to live)."""
    monkeypatch.setattr(get_settings(), "apca_api_base_url", "https://api.alpaca.markets")
    calls = []
    _patch_transport(monkeypatch, lambda req: calls.append(req) or httpx.Response(204))
    with pytest.raises(ac.PaperLockError):
        await ac.request(method, "/v2/orders/abc")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["//evil.com/v2/orders", "v2/orders", "/v1/orders",
                                  "@evil.com/v2/orders", ""])
async def test_request_rejects_paths_outside_v2(monkeypatch, path):
    calls = []
    _patch_transport(monkeypatch, lambda req: calls.append(req) or httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        await ac.request("GET", path)
    assert calls == []
