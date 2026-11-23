"""Strategies screen data: today's realized P&L, the research snapshot, and the
list/record endpoints. Real records only; nothing is sampled."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import strategy_summary as ss


def _t(exit_utc, pnl, status="closed", key="xsp_iron_fly"):
    return SimpleNamespace(strategy_key=key, status=status, pnl=pnl, exit_time=exit_utc)


def test_et_date_uses_new_york_calendar():
    # 03:00 UTC on 21 Nov is still 20 Nov in New York; naive means UTC.
    assert ss.et_date(datetime(2026, 11, 21, 3, tzinfo=timezone.utc)) == date(2026, 11, 20)
    assert ss.et_date(datetime(2026, 11, 21, 3)) == date(2026, 11, 20)
    assert ss.et_date(None) is None


def test_session_pnl_counts_only_todays_closed_trades():
    today = date(2026, 11, 20)
    trades = [
        _t(datetime(2026, 11, 20, 15, tzinfo=timezone.utc), 120.0),
        _t(datetime(2026, 11, 20, 20, tzinfo=timezone.utc), -45.5),
        _t(datetime(2026, 11, 19, 20, tzinfo=timezone.utc), 999.0),        # yesterday
        _t(None, 50.0, status="open"),                                       # still open
        _t(datetime(2026, 11, 20, 18, tzinfo=timezone.utc), None),           # no pnl
    ]
    assert ss.session_pnl(trades, today) == 74.5
    assert ss.session_pnl([], today) == 0.0


def test_paper_record_groups_legs_into_sessions():
    d = lambda day, h=21: datetime(2026, 11, day, h, tzinfo=timezone.utc)
    trades = [
        _t(d(16), 80.0), _t(d(16), -20.0),      # one fly, two legs: session +60
        _t(d(17), -150.0),                      # losing session
        _t(d(18), 40.0),
        _t(d(19), 100.0),
        _t(None, 10.0, status="open"),
    ]
    r = ss.paper_record(trades)
    assert r["source"] == "paper"
    assert r["sessions"] == 4
    assert r["wins"] == 3 and r["win_rate"] == 0.75
    assert r["net_pnl"] == 50.0
    assert r["max_drawdown"] == -150.0       # +60 peak to -90
    assert (r["first_date"], r["last_date"]) == ("2026-11-16", "2026-11-19")
    assert ss.paper_record([]) is None


def test_backtest_snapshot_prefers_longest_timeframe():
    result = {"symbol": "SPY", "date_range": {"start": "2026-01-02", "end": "2026-11-19"},
              "timeframes": {"1M": {"trade_count": 5, "win_rate": 0.4, "max_drawdown": -0.02, "total_return": 0.01},
                             "full": {"trade_count": 48, "win_rate": 0.62, "max_drawdown": -0.042, "total_return": 0.11}}}
    s = ss.backtest_snapshot(result)
    assert s["source"] == "backtest" and s["timeframe"] == "full"
    assert (s["trades"], s["win_rate"], s["max_drawdown_pct"]) == (48, 0.62, -0.042)
    assert ss.backtest_snapshot({}) is None and ss.backtest_snapshot(None) is None


@pytest.fixture
def client():
    from app.db.base import init_db
    from app.main import app
    from app.strategies.registry import seed_strategy_state

    async def _setup():
        await init_db()
        await seed_strategy_state()
    asyncio.run(_setup())
    return TestClient(app)  # no lifespan: no engine or feed side effects


def test_list_exposes_category_universe_execution_and_session_pnl(client, monkeypatch):
    from app.routers import strategies as r

    now = datetime.now(timezone.utc)

    async def recent(days):
        return [_t(now, 42.0), _t(now, 8.0), _t(now, 500.0, key="qqq_credit_spread")]

    monkeypatch.setattr(r, "_closed_trades_since", recent)
    body = client.get("/api/strategies").json()
    by = {s["key"]: s for s in body["strategies"]}
    assert body["session_date"] == ss.today_et().isoformat()
    xsp, qqq = by["xsp_iron_fly"], by["qqq_credit_spread"]
    assert xsp["category"].startswith("Options") and xsp["universe"] == ["XSP"]
    assert xsp["execution"] == "Simulated fills"
    assert xsp["session_pnl"] == 50.0
    # In-app on Alpaca paper: today's P&L comes from its own session row (none yet),
    # falling back to its closed ledger trades.
    assert qqq["execution"].startswith("Alpaca paper") and not qqq["external_runner"]
    assert qqq["session_pnl"] == 500.0
    assert qqq["universe"] == ["QQQ"]
    placeholder = next(s for s in body["strategies"] if not s["ready"])
    assert placeholder["category"] is None and placeholder["session_pnl"] is None


def test_record_endpoint_sources(client, monkeypatch):
    assert client.get("/api/strategies/qqq_credit_spread/record").json()["source"] in (
        "none", "sessions", "paper")
    assert client.get("/api/strategies/strategy_2/record").status_code == 409
    assert client.get("/api/strategies/nope/record").status_code == 404
    xsp = client.get("/api/strategies/xsp_iron_fly/record").json()
    assert xsp["source"] in ("none", "paper")


def test_session_record_uses_session_status_not_ledger_rows():
    rows = [
        {"session_date": date(2026, 11, 16), "status": "settled", "realized_pnl": 60.0},
        {"session_date": date(2026, 11, 17), "status": "closed", "realized_pnl": -150.0},
        {"session_date": date(2026, 11, 18), "status": "skipped", "realized_pnl": None},
        {"session_date": date(2026, 11, 19), "status": "missed", "realized_pnl": None},
        {"session_date": date(2026, 11, 20), "status": "killed", "realized_pnl": None},
        {"session_date": date(2026, 11, 21), "status": "open", "realized_pnl": None},
        {"session_date": date(2026, 11, 13), "status": "settled", "realized_pnl": 40.0},
    ]
    r = ss.session_record(rows)
    assert r["source"] == "sessions"
    assert r["sessions"] == 3 and r["wins"] == 2 and r["win_rate"] == round(2 / 3, 4)
    assert r["net_pnl"] == -50.0
    assert r["max_drawdown"] == -150.0      # +40, +100 peak, then -50
    assert (r["skipped"], r["killed"], r["open"]) == (2, 1, 1)
    assert (r["first_date"], r["last_date"]) == ("2026-11-13", "2026-11-17")
    only_skips = ss.session_record([{"session_date": date(2026, 11, 18), "status": "skipped"}])
    assert only_skips["sessions"] == 0 and only_skips["win_rate"] is None and only_skips["skipped"] == 1
    assert ss.session_record([]) is None


def test_xsp_record_and_pnl_come_from_its_session_store(client, monkeypatch):
    from app.routers import strategies as r
    from app.strategies.xsp_iron_fly import store

    today = ss.today_et()

    async def recent_sessions(key, limit=10):
        return [{"session_date": today, "status": "settled", "realized_pnl": 75.0},
                {"session_date": date(2026, 11, 2), "status": "skipped", "realized_pnl": None}]

    async def get_session(key, d):
        return {"session_date": d, "status": "settled", "realized_pnl": 75.0} if d == today else None

    async def recent(days):  # leg rows would double count; the session row wins
        now = datetime.now(timezone.utc)
        return [_t(now, 100.0), _t(now, -25.0), _t(now, 10.0), _t(now, -10.0)]

    monkeypatch.setattr(store, "recent_sessions", recent_sessions)
    monkeypatch.setattr(store, "get_session", get_session)
    monkeypatch.setattr(r, "_closed_trades_since", recent)
    rec = client.get("/api/strategies/xsp_iron_fly/record").json()
    assert rec["source"] == "sessions" and rec["sessions"] == 1 and rec["skipped"] == 1
    xsp = next(s for s in client.get("/api/strategies").json()["strategies"] if s["key"] == "xsp_iron_fly")
    assert xsp["session_pnl"] == 75.0


def test_xsp_unfinished_session_falls_back_to_closed_legs(client, monkeypatch):
    from app.routers import strategies as r
    from app.strategies.xsp_iron_fly import store

    async def get_session(key, d):
        return {"session_date": d, "status": "open", "realized_pnl": None}

    async def recent(days):  # one short leg already stopped out today
        return [_t(datetime.now(timezone.utc), -30.0)]

    monkeypatch.setattr(store, "get_session", get_session)
    monkeypatch.setattr(r, "_closed_trades_since", recent)
    xsp = next(s for s in client.get("/api/strategies").json()["strategies"] if s["key"] == "xsp_iron_fly")
    assert xsp["session_pnl"] == -30.0


def test_unwind_loss_day_counts_with_session_pnl():
    """A failed or skipped entry whose partial fill was unwound books its loss on the
    session row only (extra_pnl - fees, no TradeRecord). It must count."""
    rows = [
        {"session_date": date(2026, 11, 16), "status": "settled", "realized_pnl": 90.0},
        {"session_date": date(2026, 11, 17), "status": "skipped", "realized_pnl": -12.4},   # unwind loss
        {"session_date": date(2026, 11, 18), "status": "failed", "realized_pnl": -7.6},     # unwind loss
        {"session_date": date(2026, 11, 19), "status": "skipped", "realized_pnl": 0.0},     # plain skip
        {"session_date": date(2026, 11, 20), "status": "failed", "realized_pnl": None},
    ]
    r = ss.session_record(rows)
    assert r["sessions"] == 3 and r["wins"] == 1
    assert r["net_pnl"] == 70.0
    assert r["max_drawdown"] == -20.0       # 90 -> 77.6 -> 70
    assert r["skipped"] == 2                # the zero-P&L skip and the P&L-less failure
    assert (r["first_date"], r["last_date"]) == ("2026-11-16", "2026-11-18")


def test_xsp_session_pnl_includes_unwind_loss_with_no_trade_rows(client, monkeypatch):
    from app.routers import strategies as r
    from app.strategies.xsp_iron_fly import store

    async def get_session(key, d):
        return {"session_date": d, "status": "skipped", "realized_pnl": -12.4}

    async def recent(days):  # the ledger has nothing for an unwind
        return []

    monkeypatch.setattr(store, "get_session", get_session)
    monkeypatch.setattr(r, "_closed_trades_since", recent)
    xsp = next(s for s in client.get("/api/strategies").json()["strategies"] if s["key"] == "xsp_iron_fly")
    assert xsp["session_pnl"] == -12.4


def test_one_strategy_summary_error_does_not_blank_the_list(client, monkeypatch, caplog):
    """If a strategy's own store fails, the list still loads (so activate/stop stay
    reachable) and that strategy's session_pnl is None; /record falls back too."""
    from app.routers import strategies as r
    from app.strategies.xsp_iron_fly import store

    async def boom(*a, **k):
        raise RuntimeError("session store down")

    async def recent(days):
        return []

    monkeypatch.setattr(store, "get_session", boom)
    monkeypatch.setattr(store, "recent_sessions", boom)
    monkeypatch.setattr(r, "_closed_trades_since", recent)
    with caplog.at_level("WARNING", logger="trading.strategies"):
        resp = client.get("/api/strategies")
        rec = client.get("/api/strategies/xsp_iron_fly/record")
    assert resp.status_code == 200
    xsp = next(s for s in resp.json()["strategies"] if s["key"] == "xsp_iron_fly")
    assert xsp["session_pnl"] is None and xsp["ready"] is True
    assert rec.status_code == 200 and rec.json()["source"] in ("none", "paper")
    assert "xsp_iron_fly" in caplog.text
