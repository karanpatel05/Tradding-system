"""Strategy migration and HTTP controls, with no broker or order submissions."""
import asyncio
from datetime import date

from fastapi.testclient import TestClient
import pytest

from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import StrategyState
from app.engine.manager import engine_manager
from app.main import app, _resume_active_workers
from app.strategies.registry import get_strategy, seed_strategy_state
from app.strategies.xsp_iron_fly import store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_auth_token", "test-key")
    asyncio.run(init_db())
    asyncio.run(seed_strategy_state())
    return TestClient(app)


def test_new_spy_book_and_readable_xsp_archive(client):
    rows = {r["key"]: r for r in client.get("/api/strategies").json()["strategies"]}
    assert rows["spy_iron_fly"]["universe"] == ["SPY"]
    assert not rows["spy_iron_fly"]["archived"]
    assert rows["xsp_iron_fly"]["archived"]
    assert rows["xsp_iron_fly"]["universe"] == ["XSP"]
    h = {"X-App-Token": "test-key"}
    root = "/api/strategies/xsp_iron_fly"
    assert client.post(root + "/activate", headers=h).status_code == 409
    assert client.put(root + "/allocation", json={"amount": 10}, headers=h).status_code == 409
    assert client.post(root + "/stop", headers=h).status_code == 409
    assert client.get(root + "/sessions").status_code == 200
    assert client.get("/api/strategies/spy_iron_fly/sessions").status_code == 200
    spy = get_strategy("spy_iron_fly").build_runner()
    assert spy.p.ticker == "SPY" and spy.physical_delivery
    assert spy.p.close_before_settlement and spy.p.flatten_minutes_before_close == 30


@pytest.mark.asyncio
async def test_boot_only_manages_persisted_legacy_positions(monkeypatch, client):
    started = []
    async def capture(key, coroutine):
        started.append((key, coroutine.cr_code.co_name))
        coroutine.close()
    monkeypatch.setattr(engine_manager, "start_worker", capture)
    async with session_scope() as s:
        (await s.get(StrategyState, "xsp_iron_fly")).status = "scanning"
    await _resume_active_workers()
    assert not started
    async with session_scope() as s:
        assert (await s.get(StrategyState, "xsp_iron_fly")).status == "stopped"
        (await s.get(StrategyState, "xsp_iron_fly")).status = "scanning"
    await store.create_session("xsp_iron_fly", date(2025, 11, 26), status="open", ticker="XSP")
    await _resume_active_workers()
    assert started == [("xsp_iron_fly", "manage_existing")]
    async with session_scope() as s:
        assert (await s.get(StrategyState, "xsp_iron_fly")).status == "exiting"
        assert (await s.get(StrategyState, "spy_iron_fly")).status == "idle"


def test_halts_are_independent(client):
    asyncio.run(store.set_halt("legacy halt", key="xsp_iron_fly"))
    asyncio.run(store.set_halt("SPY risk", key="spy_iron_fly"))
    url = "/api/strategies/spy_iron_fly/clear-halt"
    assert client.post(url).status_code == 401
    assert client.post(url, headers={"X-App-Token": "test-key"}).json()["cleared"]
    assert asyncio.run(store.halt_reason(key="xsp_iron_fly")) == "legacy halt"
