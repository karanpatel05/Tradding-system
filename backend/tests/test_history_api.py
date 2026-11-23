"""History read service + API: paging, go-to-date snapping, index sources, 400s."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.gate import require_auth
from app.history import service
from app.history.aggregate import ET, daily_from_minutes, et_session_start_ms
from app.history.store import HistoryStore
from app.routers.dashboard import router
from app.services import chart_data

SESSION_DAYS = (date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30))


def _minute(ts, price=100.0):
    return {"time": ts, "open": price, "high": price + 1, "low": price - 1,
            "close": price + 0.5, "volume": 10.0}


def _day_minutes(day, start_min=4 * 60, end_min=20 * 60):
    base = datetime(day.year, day.month, day.day, tzinfo=ET)
    return [_minute(int((base + timedelta(minutes=m)).timestamp() * 1000), 100 + m / 100)
            for m in range(start_min, end_min)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = HistoryStore(str(tmp_path / "h.db"))
    minutes = [m for d in SESSION_DAYS for m in _day_minutes(d)]
    s.upsert_minutes("SPY", minutes)
    s.upsert_daily("SPY", daily_from_minutes(minutes))
    monkeypatch.setattr(service, "get_store", lambda: s)
    service._index_cache.clear()
    yield s
    service._index_cache.clear()


@pytest.fixture
def feeds(monkeypatch):
    calls = {"facade": [], "chart": []}
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    index_minutes = [_minute(int((now - timedelta(minutes=i)).timestamp() * 1000))
                     for i in range(600, 0, -1)]

    async def fake_facade(symbol, timeframe, start, end=None):
        calls["facade"].append((symbol, timeframe))
        return list(index_minutes)

    async def fake_get_bars(symbol, interval, now=None):
        calls["chart"].append((symbol, interval))
        bars = [_minute(1_700_000_000_000 + i * 86_400_000) for i in range(5)]
        return {"symbol": symbol, "interval": interval, "bars": bars,
                "source": "tastytrade", "note": None}

    monkeypatch.setattr(service.facade, "bars", fake_facade)
    monkeypatch.setattr(chart_data, "get_bars", fake_get_bars)
    calls["index_minutes"] = index_minutes
    return calls


@pytest.mark.asyncio
async def test_newest_page_and_flags(store, feeds):
    out = await service.get_history("SPY", "5m", limit=50)
    times = [b["time"] for b in out["bars"]]
    lo, hi = store.bounds("SPY")
    assert len(times) == 50 and times == sorted(times)
    assert times[-1] == (hi // 300_000) * 300_000
    assert out["has_more_before"] is True and out["has_more_after"] is False
    assert (out["earliest"], out["latest"]) == (lo, hi)
    assert out["symbol"] == "SPY" and out["interval"] == "5m"


@pytest.mark.asyncio
async def test_before_pages_back_without_overlap(store, feeds):
    p1 = await service.get_history("SPY", "5m", limit=50)
    p2 = await service.get_history("SPY", "5m", before=p1["bars"][0]["time"], limit=50)
    assert len(p2["bars"]) == 50
    assert p2["bars"][-1]["time"] < p1["bars"][0]["time"]
    assert p2["has_more_after"] is True


@pytest.mark.asyncio
async def test_after_pages_forward(store, feeds):
    first = await service.get_history("SPY", "1h", around=date(2020, 1, 2), limit=20)
    nxt = await service.get_history("SPY", "1h", after=first["bars"][-1]["time"], limit=20)
    assert nxt["bars"][0]["time"] > first["bars"][-1]["time"]
    assert nxt["has_more_before"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1D", "1W"])
async def test_daily_and_weekly_from_store(store, feeds, interval):
    out = await service.get_history("SPY", interval)
    assert out["bars"] and out["has_more_after"] is False
    last = et_session_start_ms(date(2026, 11, 30))
    assert out["bars"][-1]["time"] == last  # 1W: week of 11-30 starts that Monday
    assert len(out["bars"]) == (3 if interval == "1D" else 2)


@pytest.mark.asyncio
async def test_around_snaps_to_nearest_available(store, feeds):
    lo, hi = store.bounds("SPY")
    for day in (date(2026, 11, 28), date(2026, 11, 26)):  # Saturday, Thanksgiving
        out = await service.get_history("SPY", "5m", around=day, limit=40)
        times = [b["time"] for b in out["bars"]]
        target = et_session_start_ms(day)
        assert len(times) == 40
        assert any(t < target for t in times) and any(t >= target for t in times)
    old = await service.get_history("SPY", "5m", around=date(2020, 1, 2), limit=40)
    assert len(old["bars"]) == 40 and old["bars"][0]["time"] == lo
    assert old["has_more_before"] is False
    future = await service.get_history("SPY", "5m", around=date(2030, 1, 2), limit=40)
    assert len(future["bars"]) == 40
    assert future["bars"][-1]["time"] == (hi // 300_000) * 300_000
    assert future["has_more_after"] is False


@pytest.mark.asyncio
async def test_index_intraday_uses_14_day_cache(store, feeds):
    a = await service.get_history("SPX", "5m", limit=30)
    b = await service.get_history("SPX", "5m", limit=30)
    assert feeds["facade"] == [("SPX", "1m")]
    assert a["earliest"] == feeds["index_minutes"][0]["time"]
    assert a["source"] == "tastytrade" and len(b["bars"]) == 30


@pytest.mark.asyncio
async def test_index_daily_uses_existing_daily_path(store, feeds):
    out = await service.get_history("NDX", "1D")
    assert feeds["chart"] == [("NDX", "1d")] and feeds["facade"] == []
    assert len(out["bars"]) == 5


@pytest.mark.asyncio
async def test_stocks_never_touch_collector(store, feeds):
    for interval in service.HISTORY_INTERVALS:
        await service.get_history("SPY", interval, limit=20)
    assert feeds["facade"] == [] and feeds["chart"] == []


@pytest.fixture
def client(store, feeds):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_bad_requests_are_400(client):
    for qs in ("symbol=AAPL", "symbol=SPY&interval=2h", "symbol=SPY&limit=0",
               "symbol=SPY&limit=2001", "symbol=SPY&before=1&after=2"):
        assert client.get(f"/api/market/history?{qs}").status_code == 400, qs
    ok = client.get("/api/market/history?symbol=spy&limit=5")
    assert ok.status_code == 200 and len(ok.json()["bars"]) == 5
    assert client.get("/api/market/history?symbol=SPY&around=2026-11-28&limit=6").status_code == 200


def test_history_routes_are_open():
    for path in ("/api/market/history", "/api/market/history/status"):
        route = next(r for r in router.routes if getattr(r, "path", None) == path)
        assert not any(d.call is require_auth for d in route.dependant.dependencies), path


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["5m", "1h", "4h"])
async def test_around_trading_day_has_no_duplicate_buckets(store, feeds, interval):
    out = await service.get_history("SPY", interval, around=date(2026, 11, 27), limit=20)
    times = [b["time"] for b in out["bars"]]
    assert times == sorted(set(times)) and len(times) > 1


@pytest.fixture
def query_count(store, monkeypatch):
    calls = []
    real = store.minutes

    def counting(*a):
        calls.append(a)
        return real(*a)

    monkeypatch.setattr(store, "minutes", counting)
    return calls


@pytest.mark.asyncio
async def test_far_cursors_are_clamped_and_cheap(store, feeds, query_count):
    lo, hi = store.bounds("SPY")
    far_future = await service.get_history("SPY", "1m", around=date(2100, 1, 4), limit=600)
    far_past = await service.get_history("SPY", "1m", around=date(1990, 1, 2), limit=600)
    after0 = await service.get_history("SPY", "1m", after=0, limit=600)
    huge_before = await service.get_history("SPY", "1m", before=10**16, limit=600)
    huge_after = await service.get_history("SPY", "1m", after=-10**16, limit=600)
    assert far_future["bars"][-1]["time"] == hi
    assert far_past["bars"][0]["time"] == lo and after0["bars"][0]["time"] == lo
    assert huge_before["bars"][-1]["time"] == hi and huge_after["bars"][0]["time"] == lo
    assert len(query_count) <= 20


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [{}, {"before": 10**16}, {"after": 0},
                                    {"around": date(2026, 11, 27)}])
async def test_bucketing_runs_off_the_event_loop(store, feeds, monkeypatch, cursor):
    """Intraday read+bucket walks run in a worker thread, never on the event loop (F2).
    Single-row calls (the latest-bucket-time helper) are trivial and excluded."""
    import threading

    seen = []
    real = service.bucket

    def recording(minutes, interval):
        seen.append((threading.current_thread() is threading.main_thread(), len(minutes)))
        return real(minutes, interval)

    monkeypatch.setattr(service, "bucket", recording)
    out = await service.get_history("SPY", "1h", limit=50, **cursor)
    assert out["bars"]
    big = [on_loop for on_loop, n in seen if n > 1]
    assert big and not any(big), seen


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1h", "4h"])
async def test_coarse_intraday_limit_is_clamped_to_600(tmp_path, monkeypatch, feeds, interval):
    s = HistoryStore(str(tmp_path / "wide.db"))
    days = [date(2025, 1, 2) + timedelta(days=i) for i in range(400)]
    # One minute at the start of every 4h bucket (04/08/12/16 ET) of 400 calendar days.
    s.upsert_minutes("SPY", [_minute(int(datetime(d.year, d.month, d.day, h, tzinfo=ET)
                                         .timestamp() * 1000)) for d in days for h in (4, 8, 12, 16)])
    monkeypatch.setattr(service, "get_store", lambda: s)
    out = await service.get_history("SPY", interval, limit=2000)
    assert len(out["bars"]) == 600 and out["has_more_before"]
    small = await service.get_history("SPY", "15m", limit=1000)
    assert len(small["bars"]) == 1000  # finer intervals keep the full limit
