"""XSP iron fly wiring: Strategies-tab routes for the new key, kill switch keeps it
stopped across a restart, live-wiring construction, and the Tastytrade chain filter.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_xsp_api.db ../.venv/bin/python -m pytest tests/test_xsp_fly_api.py
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

KEY = "xsp_iron_fly"


@pytest.fixture
def client(monkeypatch):
    from app.config import get_settings
    from app.db.base import init_db
    from app.main import app
    from app.strategies.registry import seed_strategy_state
    monkeypatch.setattr(get_settings(), "app_auth_token", "tok")

    async def setup():
        await init_db()
        await seed_strategy_state()
    asyncio.run(setup())
    return TestClient(app)     # no lifespan: no recovery/refresher tasks, no network


def test_strategies_routes_for_xsp_never_500(client):
    h = {"X-App-Token": "tok"}
    for path in ("", f"/{KEY}", f"/{KEY}/position", f"/{KEY}/positions", f"/{KEY}/risk",
                 f"/{KEY}/backtest/last", f"/{KEY}/selection-history"):
        r = client.get(f"/api/strategies{path}", headers=h)
        assert r.status_code == 200, (path, r.text)
    assert client.get(f"/api/strategies/{KEY}").json()["overview"]["ready"] is True


def test_backtest_routes_return_note_not_an_equity_baseline(client, monkeypatch):
    import app.backtest as bt

    async def boom(*a, **k):
        raise AssertionError("equity backtester must not run for the XSP fly")
    monkeypatch.setattr(bt, "run_backtest", boom)
    r = client.get(f"/api/strategies/{KEY}/backtest")
    assert r.status_code == 200
    assert r.json()["data_available"] is False and "No in-app backtest" in r.json()["note"]
    assert client.get(f"/api/strategies/{KEY}/backtest/last").json() == {}
    assert client.get(f"/api/strategies/{KEY}/backtest/stream").status_code == 409


def test_saved_option_replays_are_read_only_and_scoped_to_xsp(client, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    base = f"/api/strategies/{KEY}/backtest/reports"
    assert client.get(base).json() == {"reports": [], "can_run": False}

    scenario = {"aggregate": {"sessions": 2, "traded": 1, "total_pnl": -20.0,
                               "win_rate": 0, "max_drawdown": -20.0, "fees": 4},
                "sessions": [{"date": "2026-08-25", "status": "settled", "pnl": -20.0,
                              "contracts": 1, "fees": 4},
                             {"date": "2026-08-26", "status": "skipped", "pnl": None,
                              "contracts": 0, "fees": 0, "data_gap": True}]}
    replay = {"basis": "modeled spread", "days": ["2026-08-25", "2026-08-26"],
              "bars_minutes": 5, "params": {"ticker": "XSP"}, "by_width": {"0.05": scenario}}
    for directory in ("xsp_bt", "xsp_bt_alpaca"):
        path = tmp_path / directory
        path.mkdir()
        (path / "results_5m.json").write_text(json.dumps(replay))
    (tmp_path / "xsp_bt" / "validate.json").write_text(json.dumps(replay))
    (tmp_path / "xsp_bt" / "results_1m_oct27.json").write_text('{"by_width": {}}')

    reports = client.get(base).json()["reports"]
    assert [r["id"] for r in reports] == ["alpaca-5m", "tastytrade-5m"]
    assert reports[0]["start"] == "2026-08-25" and reports[0]["end"] == "2026-08-26"
    report = client.get(f"{base}/alpaca-5m").json()
    assert report["by_width"]["0.05"] == scenario
    assert report["id"] == "alpaca-5m" and report["params"] == replay["params"]
    assert client.get(f"{base}/validate").status_code == 404
    assert client.get(f"{base}/tastytrade-1m-oct27").status_code == 404
    assert client.get("/api/strategies/qqq_credit_spread/backtest/reports/alpaca-5m").status_code == 404
    assert client.get("/api/strategies/strategy_2/backtest/reports").status_code == 409
    flags = {s["key"]: s["has_backtest_reports"] for s in client.get("/api/strategies").json()["strategies"]}
    assert flags[KEY] and not any(present for k, present in flags.items() if k != KEY)


def test_sessions_view_and_clear_halt(client):
    from app.strategies.xsp_iron_fly import store
    asyncio.run(store.set_halt("test breach"))
    r = client.get(f"/api/strategies/{KEY}/sessions")
    assert r.status_code == 200 and r.json()["halt"] == "test breach"
    assert client.post(f"/api/strategies/{KEY}/clear-halt").status_code == 401
    r = client.post(f"/api/strategies/{KEY}/clear-halt", headers={"X-App-Token": "tok"})
    assert r.json() == {"cleared": True}
    assert asyncio.run(store.halt_reason()) is None


def test_kill_switch_keeps_strategy_stopped_after_restart(client, monkeypatch):
    from app import main
    from app.db.base import session_scope
    from app.db.models import StrategyState
    from app.engine.manager import engine_manager
    from app.routers import dashboard

    async def set_scanning():
        async with session_scope() as s:
            (await s.get(StrategyState, KEY)).status = "scanning"
    asyncio.run(set_scanning())

    async def flat():
        return "flat"
    monkeypatch.setattr(dashboard, "flatten_all", flat)
    r = client.post("/api/kill", headers={"X-App-Token": "tok"})
    assert r.status_code == 200, r.text
    assert KEY in r.json()["strategies_stopped"]

    started = []

    async def fake_start(wid, coro):
        coro.close()
        started.append(wid)
    monkeypatch.setattr(engine_manager, "start_worker", fake_start)
    asyncio.run(main._resume_active_workers())     # what boot does
    assert KEY not in started


def test_build_runner_wires_alpaca_executor_and_live_feed():
    from app.strategies.xsp_iron_fly import XspIronFlyStrategy
    from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaFlyExecutor
    from app.strategies.xsp_iron_fly.feed import TastytradeFeed
    from app.strategies.xsp_iron_fly.params import IronFlyParams
    from app.strategies.xsp_iron_fly.runner import RealClock
    r = XspIronFlyStrategy(IronFlyParams()).build_runner()
    assert isinstance(r.ex, AlpacaFlyExecutor) and isinstance(r.feed, TastytradeFeed)
    assert isinstance(r.clock, RealClock) and r.p.mode == "alpaca_paper"


def test_tastytrade_feed_keeps_pm_xsp_contracts_only(monkeypatch):
    import tastytrade.instruments as ti

    import app.tastytrade.session as ts
    from app.strategies.xsp_iron_fly.feed import TastytradeFeed

    d = date(2026, 11, 20)
    stop = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)

    def opt(k, r, settle="PM", root="XSP"):
        return SimpleNamespace(strike_price=k, option_type=SimpleNamespace(value=r),
                               settlement_type=settle, root_symbol=root,
                               streamer_symbol=f".{root}261120{r}{k}",
                               symbol=f"{root}   261120{r}00{k}000", stops_trading_at=stop)

    async def chain(sess, sym):
        return {d: [opt(600, "C"), opt(600, "P"), opt(600, "C", settle="AM"),
                    opt(601, "C", root="XSPW")]}

    async def sess():
        return object()
    monkeypatch.setattr(ti, "get_option_chain", chain)
    monkeypatch.setattr(ts, "get_tt_session", sess)
    out = asyncio.run(TastytradeFeed().contracts("XSP", d))
    assert [(c.right, c.strike, c.symbol) for c in out] == [
        ("C", 600.0, ".XSP261120C600"), ("P", 600.0, ".XSP261120P600")]
    assert all(c.stops_trading_at == stop for c in out)
    assert asyncio.run(TastytradeFeed().contracts("XSP", date(2026, 11, 23))) == []


def test_feed_lookups_time_out_instead_of_stalling(monkeypatch):
    """facade.bars can queue behind chart candle requests with no outer timeout; the
    settlement lookup (also run at worker start) and the chain fetch must not hang."""
    import time as _time

    import tastytrade.instruments as ti

    import app.tastytrade.session as ts
    from app.marketdata import facade
    from app.strategies.xsp_iron_fly import feed as feed_mod
    from app.strategies.xsp_iron_fly.feed import TastytradeFeed

    async def hang(*a, **k):
        await asyncio.sleep(3600)

    async def sess():
        return object()
    monkeypatch.setattr(feed_mod, "LOOKUP_TIMEOUT", 0.05)
    monkeypatch.setattr(facade, "bars", hang)
    monkeypatch.setattr(ti, "get_option_chain", hang)
    monkeypatch.setattr(ts, "get_tt_session", sess)
    f = TastytradeFeed()
    monkeypatch.setattr(f, "spot", lambda ticker: None)    # force the candle fallback
    d = date(2026, 11, 20)
    close = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
    t0 = _time.monotonic()
    assert asyncio.run(f.settlement_price("XSP", d, close, close.replace(hour=22))) is None
    assert asyncio.run(f.contracts("XSP", d)) is None       # a miss, not cached
    assert ("XSP", d) not in f._contracts
    assert _time.monotonic() - t0 < 2.0


def test_sizing_equity_is_starting_equity_plus_realized_pnl():
    from datetime import date as _date

    from app.db.base import init_db, session_scope
    from app.db.models import XspFlySession
    from app.strategies.xsp_iron_fly import XspIronFlyStrategy, store
    from app.strategies.xsp_iron_fly.params import IronFlyParams
    from sqlalchemy import delete

    async def go():
        await init_db()
        async with session_scope() as s:
            await s.execute(delete(XspFlySession).where(XspFlySession.strategy_key == KEY))
        await store.create_session(KEY, _date(2026, 11, 23), status="settled", realized_pnl=1442.0,
                                   mode="alpaca_paper")
        await store.create_session(KEY, _date(2026, 11, 24), status="settled", realized_pnl=-339.5,
                                   mode="alpaca_paper")
        await store.create_session(KEY, _date(2026, 11, 25), status="killed",
                                   mode="alpaca_paper")                       # unknown: 0
        await store.create_session(KEY, _date(2026, 11, 20), status="settled", realized_pnl=999.0,
                                   mode="sim")              # old simulator: not equity
        strat = XspIronFlyStrategy(IronFlyParams(starting_equity=5000))
        return await strat._equity(), (await strat.status_view())["equity"]
    eq, shown = asyncio.run(go())
    assert eq == pytest.approx(5000 + 1442 - 339.5) and shown == pytest.approx(6102.5)
