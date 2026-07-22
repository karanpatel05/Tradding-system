"""require_auth must authenticate the CALLER via a shared app token (X-App-Token
header vs APP_AUTH_TOKEN), independent of the broker OAuth probe. Fail closed: with
no token configured, gated routes deny. Dangerous mutating routes (live-orders,
kill, activate, resume, credential-write) are gated; reads stay open.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_auth.db python -m pytest tests/test_app_auth_token.py
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth.gate import require_auth
from app.config import get_settings


@pytest.mark.asyncio
async def test_denies_when_no_token_configured():
    """Fail closed: empty APP_AUTH_TOKEN -> deny even without a header."""
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = ""
    try:
        with pytest.raises(HTTPException) as ei:
            await require_auth(x_app_token=None)
        assert ei.value.status_code == 401
    finally:
        s.app_auth_token = old


@pytest.mark.asyncio
async def test_passes_with_matching_token():
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        assert await require_auth(x_app_token="sekret") is None
    finally:
        s.app_auth_token = old


@pytest.mark.asyncio
async def test_denies_wrong_or_missing_token():
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        with pytest.raises(HTTPException):
            await require_auth(x_app_token="wrong")
        with pytest.raises(HTTPException):
            await require_auth(x_app_token=None)
    finally:
        s.app_auth_token = old


@pytest.mark.asyncio
async def test_auth_independent_of_broker_probe(monkeypatch):
    """The caller check must NOT pass just because the server's OAuth pair verifies."""
    from app.broker import status as st
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": 1e9})  # probe "ready"
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        with pytest.raises(HTTPException):
            await require_auth(x_app_token=None)  # ready probe must not authorize the caller
    finally:
        s.app_auth_token = old


# --- integration: real routes through the ASGI app --------------------------

@pytest.fixture
def client():
    """A TestClient with the real require_auth gate in force — defends against any
    other module leaking an app.dependency_overrides[require_auth] bypass. Creates the
    schema so open-route handlers that read the DB work."""
    import asyncio

    from fastapi.testclient import TestClient
    from app.auth.gate import require_auth
    from app.db.base import init_db
    from app.main import app
    asyncio.run(init_db())
    saved = app.dependency_overrides.pop(require_auth, None)
    try:
        yield TestClient(app)
    finally:
        if saved is not None:
            app.dependency_overrides[require_auth] = saved


def test_live_orders_requires_token(client):
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        assert client.request("PUT", "/api/system/live-orders",
                              json={"enabled": False}).status_code == 401
        ok = client.request("PUT", "/api/system/live-orders", json={"enabled": False},
                            headers={"X-App-Token": "sekret"})
        assert ok.status_code == 200, ok.text
    finally:
        s.app_auth_token = old


def test_credential_write_requires_token(client):
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        assert client.request("PUT", "/api/auth/tastytrade", json={}).status_code == 401
    finally:
        s.app_auth_token = old


# Dangerous writes that shape real trading / control workers: every one must reject an
# unauthenticated caller. Reads (dashboard/backtest/journal) and paper/no-op writes stay open.
DANGEROUS_WRITES = [
    ("POST", "/api/kill", None),
    ("POST", "/api/system/resume", None),
    ("PUT", "/api/system/live-orders", {"enabled": False}),
    ("PUT", "/api/auth/tastytrade", {}),
    ("POST", "/api/strategies/strategy_1/activate", None),
    ("POST", "/api/strategies/strategy_1/stop", None),
    ("PUT", "/api/strategies/strategy_1/allocation", {"amount": 1000.0}),
    ("PUT", "/api/strategies/strategy_1/contracts", {"contracts": 1}),
    ("PUT", "/api/strategies/strategy_1/tickers", {"tickers": [{"ticker": "QQQ", "contracts": 1}]}),
    ("PUT", "/api/strategies/strategy_1/direction", {"direction": "both"}),
]


@pytest.mark.parametrize("method,path,body", DANGEROUS_WRITES)
def test_dangerous_write_rejects_anonymous(client, method, path, body):
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        r = client.request(method, path, json=body) if body is not None else client.request(method, path)
        assert r.status_code == 401, f"{method} {path} should reject anonymous, got {r.status_code}"
    finally:
        s.app_auth_token = old


# Reads / paper / no-op writes must remain reachable without a token.
OPEN_ROUTES = [
    ("GET", "/api/strategies", None),
    ("GET", "/api/auth/status", None),
    ("POST", "/api/auth/disconnect", None),   # no-op, self-recovering
    ("POST", "/api/builder/payoff", {"legs": [{"action": "BUY", "right": "C", "strike": 100.0,
                                               "expiry": "2026-12-18", "qty": 1}],
                                     "net_debit": 1.0, "spot": 100.0}),
]


@pytest.mark.parametrize("method,path,body", OPEN_ROUTES)
def test_open_route_not_gated(client, method, path, body):
    s = get_settings()
    old = s.app_auth_token
    s.app_auth_token = "sekret"
    try:
        r = client.request(method, path, json=body) if body is not None else client.request(method, path)
        assert r.status_code != 401, f"{method} {path} must stay open, got 401"
    finally:
        s.app_auth_token = old
