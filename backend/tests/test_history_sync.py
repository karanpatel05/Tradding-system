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
    n0 = len(f.calls)
    await sync.sync_symbol("SPY")
    # top-ups re-read the last 2 stored minutes (a forming minute may have been stored)
    assert f.calls[n0]["start"] == datetime.fromtimestamp((first - 2 * MIN) / 1000, UTC)
    stored = store.minutes("SPY", 0, ms(now))
    assert [b["time"] for b in stored] == [b["time"] for b in bars]


@pytest.mark.asyncio
async def test_top_up_refetches_a_stored_partial_minute(store):
    """F7: if Alpaca returned the still-forming minute, the next top-up replaces it."""
    t = ms(datetime(2026, 11, 20, 15, 0, tzinfo=UTC))
    now = datetime(2026, 11, 20, 15, 0, 30, tzinfo=UTC)
    f = FakeFetch({"SPY": [bar(t - MIN, 100.0), bar(t, 100.5)]})   # forming minute, partial
    box = [now]
    sync = S.HistorySync(store, fetch=f, now=lambda: box[0])
    sync._seen.add("SPY")
    store.upsert_minutes("SPY", [bar(t - 2 * MIN, 99.0)])
    await sync.sync_symbol("SPY")
    assert store.minutes("SPY", t, t + 1)[0]["close"] == 100.5
    f.bars["SPY"] = [bar(t - MIN, 100.0), bar(t, 101.25), bar(t + MIN, 101.5)]   # final values
    box[0] = now + timedelta(minutes=2)
    n0 = len(f.calls)
    await sync.sync_symbol("SPY")
    assert f.calls[n0]["start"] == datetime.fromtimestamp((t - 2 * MIN) / 1000, UTC)
    assert store.minutes("SPY", t, t + 1)[0]["close"] == 101.25
    assert [b["time"] for b in store.minutes("SPY", 0, t + 10 * MIN)] == [
        t - 2 * MIN, t - MIN, t, t + MIN]


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
    t = et_session_start_ms(date(2024, 3, 5))   # reference day: 5 trading days back
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
async def test_late_split_adjustment_is_caught_on_following_mornings(store):
    """F5: ex-date Mon 2024-03-11. On the first morning Alpaca has not adjusted the older
    days yet; the next morning it has. Comparing a day ~5 trading days back catches it
    (the previous trading day, the ex-date itself, is post-split on both sides)."""
    pre_days = [date(2024, 3, d) for d in (4, 5, 6, 7, 8)]
    pre = [bar(et_session_start_ms(d) + i * MIN, 300.0 + i) for d in pre_days for i in range(12)]
    post = [bar(et_session_start_ms(date(2024, 3, 11)) + i * MIN, 30.0 + i) for i in range(12)]
    store.upsert_minutes("TSLA", pre + post)
    ex_morning = datetime(2024, 3, 11, 13, 0, tzinfo=UTC)          # 09:00 ET on the ex-date
    unadjusted = FakeFetch({"TSLA": pre + post})
    assert await make_sync(store, unadjusted, ex_morning).check_splits("TSLA") is False
    assert store.bounds("TSLA")[0] == pre[0]["time"]
    next_morning = datetime(2024, 3, 12, 13, 0, tzinfo=UTC)
    adjusted = FakeFetch({"TSLA": [bar(b["time"], b["close"] / 10) for b in pre] + post})
    assert await make_sync(store, adjusted, next_morning).check_splits("TSLA") is True
    assert store.bounds("TSLA") == (None, None)


@pytest.mark.asyncio
async def test_stale_iex_tail_is_not_a_split(store):
    now = datetime(2024, 3, 12, 15, 0, tzinfo=UTC)
    t = et_session_start_ms(date(2024, 3, 5))   # reference day: 5 trading days back
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


@pytest.mark.asyncio
async def test_start_sync_with_unusable_path_logs_and_returns(tmp_path, monkeypatch, caplog):
    """Opening the store happens in the background task, off the loop; a bad path is
    logged and the task ends instead of failing app startup (F4)."""
    import threading

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    threads = []

    def bad_store():
        threads.append(threading.current_thread() is threading.main_thread())
        return HistoryStore(str(blocker / "h.db"))   # parent is a regular file

    monkeypatch.setattr(S, "get_store", bad_store)
    with caplog.at_level("ERROR"):
        await asyncio.wait_for(S.start_sync(), 5)
    assert threads == [False]
    assert "history" in caplog.text.lower()


@pytest.mark.asyncio
async def test_run_forever_with_unopenable_lock_logs_and_returns(tmp_path, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")

    class FakeStore:
        path = str(blocker / "h.db")

    f = FakeFetch()
    sync = S.HistorySync(FakeStore(), fetch=f, now=lambda: datetime(2022, 1, 5, tzinfo=UTC))
    with caplog.at_level("ERROR"):
        await asyncio.wait_for(S.run_forever(sync), 5)
    assert f.calls == [] and "lock" in caplog.text.lower()


def test_lifespan_does_not_open_the_store_synchronously():
    import inspect

    from app import main

    src = inspect.getsource(main.lifespan)
    assert "get_store(" not in src and "start_sync()" in src


def test_write_chunk_is_atomic_and_merges_stored_minutes(store):
    """F9: minutes and the rebuilt daily rows land in one transaction; the daily row of a
    day already partly stored covers both the stored and the new minutes."""
    import sqlite3

    day = datetime(2022, 1, 4, tzinfo=UTC)
    first, second = session_bars(day, 3, 100.0), session_bars(day, 6, 200.0)[3:]
    sync = make_sync(store, FakeFetch(), day)
    sync._write_chunk("SPY", first)
    sync._write_chunk("SPY", second)
    (d,) = store.daily("SPY", 0, ms(day) + 86_400_000)
    assert d["open"] == first[0]["open"] and d["close"] == second[-1]["close"]
    assert d["volume"] == 60.0
    with sqlite3.connect(store.path) as c:
        c.execute("DROP TABLE bars_1d")
    later = session_bars(datetime(2022, 1, 5, tzinfo=UTC), 3)
    with pytest.raises(sqlite3.OperationalError):
        sync._write_chunk("SPY", later)
    assert store.minutes("SPY", later[0]["time"], later[-1]["time"] + 1) == []


@pytest.mark.asyncio
async def test_run_forever_runs_the_recorder_under_the_lock(store, monkeypatch):
    from app.history import recorder as R
    started, stopped = asyncio.Event(), []

    class FakeRecorder:
        def __init__(self, st):
            assert st is store

        async def run(self):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.append(1)
                raise

    monkeypatch.setattr(R, "LiveRecorder", FakeRecorder)
    sync = make_sync(store, FakeFetch(), datetime(2022, 1, 5, 22, 0, tzinfo=UTC))
    task = asyncio.create_task(S.run_forever(sync))
    await asyncio.wait_for(started.wait(), 5)
    assert isinstance(R.recorder, FakeRecorder)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [1] and R.recorder is None


@pytest.mark.asyncio
async def test_second_process_does_not_start_a_recorder(store, monkeypatch):
    from app.history import recorder as R
    monkeypatch.setattr(R, "LiveRecorder", _raise_on_init)
    fd = open(f"{store.path}.lock", "a+")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        await asyncio.wait_for(
            S.run_forever(make_sync(store, FakeFetch(), datetime(2022, 1, 5, 22, 0, tzinfo=UTC))), 5)
    finally:
        fd.close()
    assert R.recorder is None


def _raise_on_init(*a, **kw):
    raise AssertionError("recorder must not start without the lock")


@pytest.mark.asyncio
async def test_recorder_that_cannot_start_does_not_stop_the_sync(store, monkeypatch, caplog):
    from app.history import recorder as R
    monkeypatch.setattr(R, "LiveRecorder", _raise_on_init)
    f = FakeFetch()
    task = asyncio.create_task(S.run_forever(
        make_sync(store, f, datetime(2022, 1, 5, 22, 0, tzinfo=UTC))))
    for _ in range(200):
        if f.calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert f.calls and R.recorder is None and "recorder" in caplog.text.lower()


# ---- end-of-day finalize ------------------------------------------------------------------

from app.history.live_bars import Trade as _Trade  # noqa: E402

D1, D2 = date(2026, 11, 18), date(2026, 11, 19)     # consecutive trading days (Wed, Thu)


def _et(h, m, s=0, day=D1):
    return int(datetime(day.year, day.month, day.day, h, m, s, tzinfo=S.ET).timestamp() * 1000)


def _fill_rth_minutes(store, symbol, day, skip=frozenset()):
    t0 = et_session_start_ms(day)
    store.upsert_minutes(symbol, [bar(t0 + i * MIN) for i in range(390)
                                  if t0 + i * MIN not in skip])


def _trade(ts):
    return _Trade("SPY", ts, 1, 1.0, 1)


def _eod_sync(store, fetch, now):
    return make_sync(store, fetch, now)


@pytest.mark.asyncio
async def test_finalize_deletes_trades_when_day_complete(store):
    f = FakeFetch()
    _fill_rth_minutes(store, "SPY", D1)
    store.insert_trades([_trade(_et(10, 0, 1))])
    await _eod_sync(store, f, datetime(2026, 11, 19, 15, tzinfo=UTC)).finalize_day(D1)
    assert store.live_days() == []
    assert not [c for c in f.calls if c["symbol"] == "SPY"]


@pytest.mark.asyncio
async def test_finalize_refetches_missing_once_and_notes_gaps(store, monkeypatch):
    monkeypatch.setattr(S, "stock_symbols", lambda: ("SPY",))
    f = FakeFetch()
    _fill_rth_minutes(store, "SPY", D1, skip={_et(11, 0)})
    store.insert_trades([_trade(_et(10, 0, 1))])
    await _eod_sync(store, f, datetime(2026, 11, 19, 15, tzinfo=UTC)).finalize_day(D1)
    assert len(f.calls) == 1 and f.calls[0]["feed"] == "sip"
    assert store.get_state("SPY")["note"] == "1 minutes missing on 2026-11-18"
    assert store.live_days() == []


@pytest.mark.asyncio
async def test_finalize_fills_gap_from_official_bars_only(store, monkeypatch):
    monkeypatch.setattr(S, "stock_symbols", lambda: ("SPY",))
    gap = _et(11, 0)
    f = FakeFetch({"SPY": [bar(gap, 55.0)]})
    _fill_rth_minutes(store, "SPY", D1, skip={gap})
    await _eod_sync(store, f, datetime(2026, 11, 19, 15, tzinfo=UTC)).finalize_day(D1)
    assert [m["close"] for m in store.minutes("SPY", gap, gap + MIN)] == [55.0]
    assert not (store.get_state("SPY") or {}).get("note")


@pytest.mark.asyncio
async def test_finalize_fetch_failure_keeps_day(store, monkeypatch):
    monkeypatch.setattr(S, "stock_symbols", lambda: ("SPY",))
    f = FakeFetch(raises={"SPY": RuntimeError("boom")})
    _fill_rth_minutes(store, "SPY", D1, skip={_et(11, 0)})
    store.insert_trades([_trade(_et(10, 0, 1))])
    await _eod_sync(store, f, datetime(2026, 11, 19, 15, tzinfo=UTC)).finalize_day(D1)
    assert store.live_days() == [D1]
    assert "boom" in store.get_state("SPY")["note"]


@pytest.mark.asyncio
async def test_startup_finalizes_earlier_days(store):
    store.insert_trades([_trade(_et(10, 0, 1))])
    await _eod_sync(store, FakeFetch(), datetime(2026, 11, 19, 15, tzinfo=UTC)).run_once()
    assert D1 not in store.live_days()


@pytest.mark.asyncio
async def test_today_not_finalized_before_2015(store):
    store.insert_trades([_trade(_et(10, 0, 1, day=D2))])
    await _eod_sync(store, FakeFetch(), datetime(2026, 11, 20, 0, tzinfo=UTC)).run_once()  # 19:00 ET
    assert store.live_days() == [D2]


@pytest.mark.asyncio
async def test_today_finalized_after_2015(store):
    store.insert_trades([_trade(_et(10, 0, 1, day=D2))])
    await _eod_sync(store, FakeFetch(), datetime(2026, 11, 20, 1, 16, tzinfo=UTC)).run_once()  # 20:16 ET
    assert store.live_days() == []


# ---- newest consolidated (SIP) minute ----------------------------------------------------

@pytest.mark.asyncio
async def test_sync_records_sip_latest_from_sip_bars_only(store):
    now1 = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    box = [now1]
    store.upsert_minutes("SPY", [bar(ms(now1 - timedelta(hours=3)))])
    sip_t = ms(now1 - timedelta(minutes=30))
    tail_t = ms(now1 - timedelta(minutes=5))

    def deny(call, n):
        if call["feed"] == "sip" and call["end"] > box[0] - timedelta(minutes=16):
            raise AlpacaDataError(403, "subscription does not permit querying recent SIP data")

    f = FakeFetch({"SPY": [bar(sip_t, 78.0), bar(tail_t, 79.0)]}, {"SPY": deny},
                  iex={"SPY": [bar(tail_t, 77.0)]})
    sync = S.HistorySync(store, fetch=f, now=lambda: box[0])
    await sync.sync_symbol("SPY")
    assert store.bounds("SPY")[1] == tail_t           # the IEX tail is stored ...
    assert store.sip_latest("SPY") == sip_t           # ... but does not move sip_latest
    box[0] = now1 + timedelta(minutes=30)
    await sync.sync_symbol("SPY")
    assert store.sip_latest("SPY") == tail_t          # SIP now covers the old tail


@pytest.mark.asyncio
async def test_finalize_sip_refetch_records_sip_latest(store, monkeypatch):
    monkeypatch.setattr(S, "stock_symbols", lambda: ("SPY",))
    gap = _et(11, 0)
    f = FakeFetch({"SPY": [bar(gap, 55.0)]})
    _fill_rth_minutes(store, "SPY", D1, skip={gap})
    await _eod_sync(store, f, datetime(2026, 11, 19, 15, tzinfo=UTC)).finalize_day(D1)
    assert store.sip_latest("SPY") == gap
