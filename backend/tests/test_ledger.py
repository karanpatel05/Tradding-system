"""App-owned ledger: only `tsys-` orders count; mocked Alpaca HTTP."""
import httpx
import pytest

from app.broker import alpaca_client as ac
from app.broker import ledger
from app.config import get_settings


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "PK")
    monkeypatch.setattr(s, "apca_api_secret_key", "sk")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")


def _mock(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


def _orders(monkeypatch, orders):
    _mock(monkeypatch, lambda req: httpx.Response(200, json=orders))


def _o(coid, sym, side, qty, filled, **kw):
    return {"id": coid, "client_order_id": coid, "symbol": sym, "side": side,
            "qty": str(qty), "filled_qty": str(filled), "legs": None, **kw}


@pytest.mark.asyncio
async def test_ledger_ignores_untagged_orders(monkeypatch):
    _orders(monkeypatch, [_o("tsys-a", "AAPL", "buy", 10, 10),
                          _o("manual-1", "AAPL", "buy", 50, 50)])
    assert await ledger.app_net_positions() == {"AAPL": 10}


@pytest.mark.asyncio
async def test_ledger_uses_filled_qty_for_partial_fills(monkeypatch):
    _orders(monkeypatch, [_o("tsys-a", "AAPL", "buy", 10, 4)])
    assert await ledger.app_net_positions() == {"AAPL": 4}


@pytest.mark.asyncio
async def test_ledger_bracket_children_net_out(monkeypatch):
    tp = _o("c1", "AAPL", "sell", 5, 5)
    sl = _o("c2", "AAPL", "sell", 5, 0)
    _orders(monkeypatch, [_o("tsys-b", "AAPL", "buy", 5, 5, order_class="bracket",
                             legs=[tp, sl])])
    assert await ledger.app_net_positions() == {}


@pytest.mark.asyncio
async def test_ledger_option_symbols_unpadded(monkeypatch):
    _orders(monkeypatch, [_o("tsys-o", "QQQ261117C00660000", "buy", 1, 1)])
    assert await ledger.app_net_positions() == {"QQQ261117C00660000": 1}


@pytest.mark.asyncio
async def test_ledger_mleg_uses_leg_filled_qty_not_ratio(monkeypatch):
    legs = [{"symbol": "QQQ261117C00660000", "side": "buy", "ratio_qty": "2", "filled_qty": "4"},
            {"symbol": "QQQ261117C00670000", "side": "sell", "ratio_qty": "1", "filled_qty": "2"}]
    _orders(monkeypatch, [{"id": "m", "client_order_id": "tsys-m", "order_class": "mleg",
                           "symbol": None, "legs": legs, "filled_qty": "2"}])
    assert await ledger.app_net_positions() == {"QQQ261117C00660000": 4,
                                                "QQQ261117C00670000": -2}


@pytest.mark.asyncio
async def test_app_orders_pages_by_until(monkeypatch):
    seen = []
    full = [{"id": f"x{i}", "client_order_id": f"tsys-{i}", "submitted_at": f"2026-01-01T00:{59 - i // 60:02d}:{59 - i % 60:02d}Z"}
            for i in range(500)]

    def handler(req):
        seen.append(req.url.params.get("until"))
        if req.url.params.get("until") is None:
            return httpx.Response(200, json=full)
        return httpx.Response(200, json=[{"id": "last", "client_order_id": "tsys-last"}])
    _mock(monkeypatch, handler)
    out = await ledger.app_orders("all")
    assert len(out) == 501 and seen[0] is None and seen[1] == full[-1]["submitted_at"]
