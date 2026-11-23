import asyncio
import fcntl
from datetime import date, datetime, timedelta, timezone

import pytest

from app.history import sync as S
from app.history.aggregate import et_session_start_ms
from app.history.alpaca_bars import AlpacaDataError
from app.history.store import HistoryStore

UTC = timezone.utc
MIN = 60_000


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def bar(t_ms: int, close: float = 100.0) -> dict:
    return {"time": t_ms, "open": close, "high": close, "low": close,
            "close": close, "volume": 10.0}


def session_bars(day: datetime, n: int = 5, close: float = 100.0) -> list[dict]:
    """n minutes from 09:30 ET (14:30 UTC in winter) on the UTC-dated `day`."""
    t0 = ms(day.replace(hour=14, minute=30, second=0, microsecond=0))
    return [bar(t0 + i * MIN, close + i) for i in range(n)]


class FakeFetch:
    def __init__(self, bars=None, raises=None, iex=None):
        self.iex = iex
        self.bars = bars or {}          # symbol -> list[bar]
        self.raises = raises or {}      # symbol -> exception | callable(call_no, kw)
        self.calls = []

    async def __call__(self, symbol, start, end, *, feed="sip", limiter=None, **kw):
        self.calls.append({"symbol": symbol, "start": start, "end": end,
                           "feed": feed, "limiter": limiter})
        r = self.raises.get(symbol)
        if callable(r):
            r(self.calls[-1], len([c for c in self.calls if c["symbol"] == symbol]))
        elif r is not None:
            raise r
        lo, hi = ms(start), ms(end)
        src = self.iex if (feed == "iex" and self.iex is not None) else self.bars
        return [b for b in src.get(symbol, []) if lo <= b["time"] <= hi]


@pytest.fixture
def store(tmp_path):
    return HistoryStore(str(tmp_path / "h.db"))


def make_sync(store, fetch, now):
    return S.HistorySync(store, fetch=fetch, now=lambda: now)


@pytest.mark.asyncio
async def test_first_sync_starts_at_2022_and_builds_daily(store):
    now = datetime(2022, 1, 5, 22, 0, tzinfo=UTC)
    bars = session_bars(datetime(2022, 1, 3, tzinfo=UTC)) + \
        session_bars(datetime(2022, 1, 4, tzinfo=UTC))
    f = FakeFetch({"SPY": bars})
    n = await make_sync(store, f, now).sync_symbol("SPY")
    assert n == 10
    assert f.calls[0]["start"] == S.HISTORY_START and f.calls[0]["feed"] == "sip"
    assert f.calls[0]["limiter"] is not None
    daily = store.daily("SPY", 0, ms(now))
    assert [d["time"] for d in daily] == [
        et_session_start_ms(date(2022, 1, 3)), et_session_start_ms(date(2022, 1, 4))]
    assert store.get_state("SPY")["status"] == "ok"


@pytest.mark.asyncio
async def test_backfill_resumes_after_crash(store):
    now = datetime(2022, 3, 15, 22, 0, tzinfo=UTC)
    bars = (session_bars(datetime(2022, 1, 5, tzinfo=UTC))
            + session_bars(datetime(2022, 2, 10, tzinfo=UTC))
            + session_bars(datetime(2022, 3, 1, tzinfo=UTC)))
    state = {"boom": True}

    def boom(call, n):
        if n == 2 and state["boom"]:
            state["boom"] = False
            raise RuntimeError("restart")

    f = FakeFetch({"SPY": bars}, {"SPY": boom})
    sync = make_sync(store, f, now)
    with pytest.raises(RuntimeError):
        await sync.sync_symbol("SPY")
    first = store.bounds("SPY")[1]
    assert first == bars[4]["time"]          # only the first chunk landed
    await sync.sync_symbol("SPY")
    assert f.calls[-3]["start"] == datetime.fromtimestamp((first + MIN) / 1000, UTC)
    stored = store.minutes("SPY", 0, ms(now))
    assert [b["time"] for b in stored] == [b["time"] for b in bars]


@pytest.mark.asyncio
async def test_recent_sip_denied_uses_iex_tail(store):
    now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    store.upsert_minutes("SPY", [bar(ms(now - timedelta(hours=3)))])
    cutoff = now - timedelta(minutes=16)

    def deny(call, n):
        if call["feed"] == "sip" and call["end"] > cutoff:
            raise AlpacaDataError(403, "subscription does not permit querying recent SIP data")

    tail = bar(ms(now - timedelta(minutes=5)), 77.0)
    f = FakeFetch({"SPY": [tail]}, {"SPY": deny})
    await make_sync(store, f, now).sync_symbol("SPY")
    ok_sip = [c for c in f.calls if c["feed"] == "sip" and c["end"] <= cutoff]
    iex = [c for c in f.calls if c["feed"] == "iex"]
    assert len(ok_sip) == 1 and ok_sip[0]["end"] == cutoff
    assert len(iex) == 1 and iex[0]["start"] == cutoff and iex[0]["end"] == now
    assert store.bounds("SPY")[1] == tail["time"]


@pytest.mark.asyncio
async def test_iex_tail_is_replaced_by_sip_next_sync(store):
    now1 = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    box = [now1]
    store.upsert_minutes("SPY", [bar(ms(now1 - timedelta(hours=3)))])
    cutoff1 = now1 - timedelta(minutes=16)
    t = ms(now1 - timedelta(minutes=5))

    def deny(call, n):
        if call["feed"] == "sip" and call["end"] > box[0] - timedelta(minutes=16):
            raise AlpacaDataError(403, "subscription does not permit querying recent SIP data")

    f = FakeFetch({"SPY": [bar(t, 78.0)]}, {"SPY": deny}, iex={"SPY": [bar(t, 77.0)]})
    sync = S.HistorySync(store, fetch=f, now=lambda: box[0])
    await sync.sync_symbol("SPY")
    assert store.minutes("SPY", t, t + 1)[0]["close"] == 77.0
    box[0] = now1 + timedelta(minutes=30)
    f.calls.clear()
    await sync.sync_symbol("SPY")
    assert any(c["feed"] == "sip" and c["start"] == cutoff1 for c in f.calls)
    assert store.minutes("SPY", t, t + 1)[0]["close"] == 78.0


@pytest.mark.asyncio
async def test_first_sync_after_restart_rechecks_last_20_minutes(store):
    latest = ms(datetime(2022, 1, 5, 15, 0, tzinfo=UTC))
    store.upsert_minutes("SPY", [bar(latest)])
    now = datetime(2022, 1, 5, 22, 0, tzinfo=UTC)
    f = FakeFetch()
    await make_sync(store, f, now).sync_symbol("SPY")
    assert f.calls[0]["start"] == datetime.fromtimestamp(latest / 1000, UTC) - timedelta(minutes=20)


@pytest.mark.asyncio
async def test_split_triggers_full_redownload(store):
    now = datetime(2024, 3, 12, 15, 0, tzinfo=UTC)
    t = et_session_start_ms(date(2024, 3, 11))
    old = [bar(t + i * MIN, 298.41 + i) for i in range(12)]
    store.upsert_minutes("TSLA", old)
    f = FakeFetch({"TSLA": [bar(b["time"], b["close"] / 10) for b in old]})
    assert await make_sync(store, f, now).check_splits("TSLA") is True
    assert store.bounds("TSLA") == (None, None)
    # unchanged prices -> no wipe
    store.upsert_minutes("TSLA", old)
    f2 = FakeFetch({"TSLA": old})
    assert await make_sync(store, f2, now).check_splits("TSLA") is False
    assert store.bounds("TSLA")[0] == t


@pytest.mark.asyncio
async def test_stale_iex_tail_is_not_a_split(store):
    now = datetime(2024, 3, 12, 15, 0, tzinfo=UTC)
    t = et_session_start_ms(date(2024, 3, 11))
    stored = [bar(t + i * MIN, 100.0) for i in range(390)]
    fetched = [bar(b["time"], b["close"] * (1.0005 if i >= 374 else 1.0))
               for i, b in enumerate(stored)]
    store.upsert_minutes("SPY", stored)
    f = FakeFetch({"SPY": fetched})
    assert await make_sync(store, f, now).check_splits("SPY") is False
    assert len(store.minutes("SPY", 0, t + 400 * MIN)) == 390


@pytest.mark.asyncio
async def test_bars_outside_extended_hours_are_not_stored(store):
    now = datetime(2022, 1, 5, 22, 0, tzinfo=UTC)
    et = S.ET
    times = [ms(datetime(2022, 1, 4, h, m, tzinfo=et)) for h, m in
             ((3, 59), (4, 0), (19, 59), (20, 0))]
    f = FakeFetch({"SPY": [bar(t) for t in times]})
    await make_sync(store, f, now).sync_symbol("SPY")
    assert [b["time"] for b in store.minutes("SPY", 0, ms(now))] == [times[1], times[2]]


@pytest.mark.asyncio
async def test_auth_and_rate_errors_are_contained(store):
    now = datetime(2022, 1, 5, 22, 0, tzinfo=UTC)
    day = session_bars(datetime(2022, 1, 3, tzinfo=UTC))
    f = FakeFetch({"TSLA": day, "NVDA": day},
                  {"SPY": AlpacaDataError(401, "unauthorized"),
                   "QQQ": AlpacaDataError(429, "too many requests")})
    sync = make_sync(store, f, now)
    await sync.run_once()
    assert store.get_state("SPY")["status"] == "error" and "401" in store.get_state("SPY")["note"]
    assert store.get_state("QQQ")["status"] == "error" and "429" in store.get_state("QQQ")["note"]
    assert store.get_state("TSLA")["status"] == "ok"
    assert store.bounds("NVDA")[1] is not None
    assert sync.errors == 2


def test_next_delay():
    et = S.ET
    mon_noon = datetime(2026, 11, 23, 12, 0, tzinfo=et)
    assert S.next_delay(mon_noon) == 60
    assert S.next_delay(datetime(2026, 11, 23, 18, 0, tzinfo=et)) == 300
    assert S.next_delay(datetime(2026, 11, 23, 5, 0, tzinfo=et)) == 300
    sat = datetime(2026, 11, 21, 12, 0, tzinfo=et)
    assert S.next_delay(sat) == (datetime(2026, 11, 23, 4, 0, tzinfo=et) - sat).total_seconds()
    late = datetime(2026, 11, 23, 21, 0, tzinfo=et)
    assert S.next_delay(late) == 7 * 3600
    early = datetime(2026, 11, 23, 2, 0, tzinfo=et)
    assert S.next_delay(early) == 2 * 3600


@pytest.mark.asyncio
async def test_second_process_stands_down(store):
    f = FakeFetch()
    fd = open(f"{store.path}.lock", "a+")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        await asyncio.wait_for(
            S.run_forever(make_sync(store, f, datetime(2022, 1, 5, 22, 0, tzinfo=UTC))), 5)
    finally:
        fd.close()
    assert f.calls == []


def test_history_status(store, monkeypatch):
    monkeypatch.setattr(S, "get_store", lambda: store)
    store.upsert_minutes("SPY", [bar(1_000 * MIN)])
    store.set_state("SPY", status="ok", note="")
    st = S.history_status()
    assert set(st) == {"SPY", "QQQ", "TSLA", "NVDA"}
    assert st["SPY"] == {"earliest": 1_000 * MIN, "latest": 1_000 * MIN, "status": "ok", "note": ""}
    assert st["QQQ"]["earliest"] is None and st["QQQ"]["status"] == "pending"
