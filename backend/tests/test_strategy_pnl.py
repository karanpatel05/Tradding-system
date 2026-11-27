"""Per-strategy year / month / day realized P&L: the pure calendar helpers, the
GET /api/strategies/{key}/pnl route and the list's month/year figures.

Run: DATABASE_URL=sqlite+aiosqlite:///./_t_pnl.db ../.venv/bin/python -m pytest tests/test_strategy_pnl.py
"""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import strategy_summary as ss


def _t(exit_utc, pnl, status="closed", key="strategy_x"):
    return SimpleNamespace(strategy_key=key, status=status, pnl=pnl, exit_time=exit_utc)


def test_daily_from_sessions_prices_only_counted_sessions():
    rows = [
        {"session_date": date(2026, 3, 2), "status": "settled", "realized_pnl": 60.0},
        {"session_date": date(2026, 3, 3), "status": "missed", "realized_pnl": None},
        {"session_date": date(2026, 3, 4), "status": "skipped", "realized_pnl": -12.5},  # unwind loss
        {"session_date": date(2026, 3, 5), "status": "skipped", "realized_pnl": 0.0},
    ]
    d = ss.daily_from_sessions(rows)
    assert d["2026-03-02"] == {"date": "2026-03-02", "pnl": 60.0, "status": "settled"}
    assert d["2026-03-03"]["pnl"] is None and d["2026-03-03"]["status"] == "missed"
    assert d["2026-03-04"]["pnl"] == -12.5
    assert d["2026-03-05"]["pnl"] is None


def test_daily_from_trades_groups_by_new_york_exit_date():
    trades = [
        _t(datetime(2026, 4, 7, 15, tzinfo=timezone.utc), 40.0),
        _t(datetime(2026, 4, 7, 19, tzinfo=timezone.utc), -15.0),
        _t(datetime(2026, 4, 8, 2, tzinfo=timezone.utc), 5.0),     # still Apr 7 in New York
        _t(None, 99.0, status="open"),
        _t(datetime(2026, 4, 9, 15, tzinfo=timezone.utc), None),
    ]
    d = ss.daily_from_trades(trades)
    assert d == {"2026-04-07": {"date": "2026-04-07", "pnl": 30.0, "status": "closed"}}


def test_pnl_calendar_rolls_days_into_months_and_year():
    days = {
        "2025-12-30": {"date": "2025-12-30", "pnl": 500.0, "status": "settled"},
        "2026-01-05": {"date": "2026-01-05", "pnl": 100.0, "status": "settled"},
        "2026-01-06": {"date": "2026-01-06", "pnl": -40.0, "status": "closed"},
        "2026-01-07": {"date": "2026-01-07", "pnl": None, "status": "missed"},
        "2026-03-10": {"date": "2026-03-10", "pnl": 25.0, "status": "settled"},
    }
    today = date(2026, 3, 11)
    c = ss.pnl_calendar(days, 2026, today)
    assert c["year"] == 2026 and c["years"] == [2025, 2026]
    assert c["total"] == 85.0 and c["sessions"] == 3 and c["wins"] == 2
    jan, feb, mar = c["months"][0], c["months"][1], c["months"][2]
    assert (jan["pnl"], jan["sessions"], jan["wins"]) == (60.0, 2, 1)
    assert (feb["pnl"], feb["sessions"]) == (0.0, 0)
    assert mar["pnl"] == 25.0 and len(c["months"]) == 12
    assert [r["date"] for r in c["days"]] == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-03-10"]
    assert c["month_to_date"] == 25.0 and c["year_to_date"] == 85.0 and c["today"] is None
    prior = ss.pnl_calendar(days, 2025, today)
    assert prior["total"] == 500.0 and prior["year_to_date"] == 85.0   # to-date stays on today


def test_pnl_calendar_fills_today_with_partial_realized_pnl():
    today = date(2026, 11, 24)
    open_row = {"2026-11-24": {"date": "2026-11-24", "pnl": None, "status": "open"}}
    c = ss.pnl_calendar(open_row, 2026, today, today_pnl=-30.0)
    assert c["today"] == -30.0 and c["month_to_date"] == -30.0
    assert c["days"][0]["partial"] is True and c["days"][0]["status"] == "open"
    # A settled session keeps its own figure; a zero with no row adds nothing.
    settled = {"2026-11-24": {"date": "2026-11-24", "pnl": 75.0, "status": "settled"}}
    assert ss.pnl_calendar(settled, 2026, today, today_pnl=-30.0)["today"] == 75.0
    assert ss.pnl_calendar({}, 2026, today, today_pnl=0.0)["days"] == []


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


def test_pnl_route_reads_the_xsp_session_store(client, monkeypatch):
    from app.routers import strategies as r
    from app.strategies.xsp_iron_fly import store

    today = ss.today_et()
    last_year = date(today.year - 1, 6, 15)

    async def recent_sessions(key, limit=10):
        assert limit >= 1000                     # the whole history, not the last 10
        return [{"session_date": today, "status": "open", "realized_pnl": None},
                {"session_date": date(today.year, 1, 2), "status": "settled", "realized_pnl": 120.0},
                {"session_date": last_year, "status": "closed", "realized_pnl": -80.0}]

    async def get_session(key, d):
        return {"session_date": d, "status": "open", "realized_pnl": None} if d == today else None

    async def recent(days):  # one short leg stopped out today
        return [_t(datetime.now(timezone.utc), -30.0, key="xsp_iron_fly")]

    monkeypatch.setattr(store, "recent_sessions", recent_sessions)
    monkeypatch.setattr(store, "get_session", get_session)
    monkeypatch.setattr(r, "_closed_trades_since", recent)

    body = client.get("/api/strategies/xsp_iron_fly/pnl").json()
    assert body["source"] == "sessions" and body["year"] == today.year
    assert body["today"] == -30.0
    assert body["year_to_date"] == round(120.0 - 30.0, 2)
    assert body["years"] == [today.year - 1, today.year]
    assert client.get(f"/api/strategies/xsp_iron_fly/pnl?year={today.year - 1}").json()["total"] == -80.0

    xsp = next(s for s in client.get("/api/strategies").json()["strategies"] if s["key"] == "xsp_iron_fly")
    assert xsp["session_pnl"] == -30.0
    assert xsp["year_pnl"] == body["year_to_date"] and xsp["month_pnl"] == body["month_to_date"]


def test_pnl_route_gates(client):
    assert client.get("/api/strategies/nope/pnl").status_code == 404
    assert client.get("/api/strategies/strategy_2/pnl").status_code == 409
    assert client.get("/api/strategies/xsp_iron_fly/pnl?year=1999").status_code == 422
    placeholder = next(s for s in client.get("/api/strategies").json()["strategies"] if not s["ready"])
    assert placeholder["month_pnl"] is None and placeholder["year_pnl"] is None
