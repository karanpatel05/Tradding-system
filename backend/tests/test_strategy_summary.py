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
    # External runner: its trades are not the app's, so no P&L is claimed.
    assert qqq["session_pnl"] is None and qqq["execution"] == "External runner"
    assert qqq["universe"] == ["QQQ"]
    placeholder = next(s for s in body["strategies"] if not s["ready"])
    assert placeholder["category"] is None and placeholder["session_pnl"] is None


def test_record_endpoint_sources(client, monkeypatch):
    assert client.get("/api/strategies/qqq_credit_spread/record").json()["source"] == "external"
    assert client.get("/api/strategies/strategy_2/record").status_code == 409
    assert client.get("/api/strategies/nope/record").status_code == 404
    xsp = client.get("/api/strategies/xsp_iron_fly/record").json()
    assert xsp["source"] in ("none", "paper")
