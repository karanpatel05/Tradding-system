"""Background job that keeps the local 1-minute store current from Alpaca (live service only).

Resumes from the last stored minute, so a restart mid-backfill loses nothing. Only the process
holding `<db>.lock` runs it. Errors are recorded per symbol and never raised."""
from __future__ import annotations

import asyncio
import fcntl
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.history import recorder as recorder_mod
from app.history.aggregate import daily_from_minutes, et_session_start_ms
from app.history.alpaca_bars import AlpacaDataError, RateLimiter, fetch_minutes
from app.history.live_bars import stock_symbols
from app.history.store import HistoryStore, get_store
from app.services.market import _is_trading_day

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
HISTORY_START = datetime(2022, 1, 3, 14, 30, tzinfo=UTC)

_MINUTE_MS = 60_000
_CHUNK = timedelta(days=30)              # one store write (and crash-resume point) per chunk
_TOP_UP_OVERLAP = 2                      # minutes each top-up re-fetches (upsert by ts)
_SIP_DELAY = timedelta(minutes=16)       # free plans may not query the latest 15 min of SIP
_SPLIT_TOLERANCE = 0.0001
_SPLIT_MIN_OVERLAP = 10
_SPLIT_LOOKBACK_DAYS = 5                 # reference day for the daily split check, in trading days
_MAX_BACKOFF = 900.0


def _from_ms(t: int) -> datetime:
    return datetime.fromtimestamp(t / 1000, UTC)


def _day_range_ms(day: date) -> tuple[int, int]:
    lo = datetime.combine(day, dtime(0, 0), tzinfo=ET)
    hi = datetime.combine(day + timedelta(days=1), dtime(0, 0), tzinfo=ET)
    return int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)


def _short_reason(exc: Exception) -> str:
    if isinstance(exc, AlpacaDataError):
        hint = {401: "check API keys", 403: "access denied", 429: "rate limited"}.get(exc.status)
        return f"Alpaca HTTP {exc.status}" + (f" ({hint})" if hint else "")
    return f"{type(exc).__name__}: {str(exc)[:80]}".rstrip(": ")


class HistorySync:
    def __init__(self, store: HistoryStore, fetch=fetch_minutes,
                 now=lambda: datetime.now(UTC)):
        self.store = store
        self._fetch = fetch
        self._now = now
        self._limiter = RateLimiter(per_minute=180)   # one cap shared by every request
        self._seen: set[str] = set()                  # symbols synced by this instance
        self._iex_since: dict[str, int] = {}          # symbol -> start ms of stored IEX tail
        self.errors = 0                               # symbols that failed in the last run_once

    async def _store(self, fn, *args, **kw):
        return await asyncio.to_thread(fn, *args, **kw)

    def _write_chunk(self, symbol: str, bars: list[dict], sip: bool = False) -> int:
        """Upsert a batch and rebuild the daily rows of every ET date it touched, in one
        store transaction (each day's row is built from its stored minutes + the batch).
        `sip=True` (consolidated bars, never the IEX tail) also records the newest one as
        the symbol's sip_latest in that transaction."""
        bars = [b for b in bars
                if dtime(4, 0) <= datetime.fromtimestamp(b["time"] / 1000, ET).time()
                < dtime(20, 0)]
        by_day: dict[date, list[dict]] = {}
        for b in bars:
            by_day.setdefault(datetime.fromtimestamp(b["time"] / 1000, ET).date(), []).append(b)
        rows: list[dict] = []
        for day in sorted(by_day):
            lo, hi = _day_range_ms(day)
            merged = {m["time"]: m for m in self.store.minutes(symbol, lo, hi)}
            merged.update({int(b["time"]): b for b in by_day[day]})
            rows += daily_from_minutes([merged[t] for t in sorted(merged)])
        sip_latest = max((int(b["time"]) for b in bars), default=None) if sip else None
        return self.store.upsert_minutes_and_daily(symbol, bars, rows, sip_latest=sip_latest)

    async def _pull(self, symbol: str, start: datetime, end: datetime, feed: str) -> list[dict]:
        return await self._fetch(symbol, start, end, feed=feed, limiter=self._limiter)

    async def sync_symbol(self, symbol: str) -> int:
        now = self._now()
        _, latest = await self._store(self.store.bounds, symbol)
        first = symbol not in self._seen
        self._seen.add(symbol)
        if latest is None:
            cursor = HISTORY_START
        elif first:   # the IEX-tail map is lost on restart: re-check the last 20 minutes
            cursor = max(HISTORY_START, _from_ms(latest) - timedelta(minutes=20))
        else:   # re-read the last 2 stored minutes: a forming minute may have been stored
            cursor = max(HISTORY_START, _from_ms(latest - _TOP_UP_OVERLAP * _MINUTE_MS))
            if symbol in self._iex_since:
                cursor = min(cursor, _from_ms(self._iex_since[symbol]))
        sip_end = now
        written = 0
        while cursor < sip_end:
            end = min(cursor + _CHUNK, sip_end)
            try:
                bars = await self._pull(symbol, cursor, end, "sip")
            except AlpacaDataError as exc:
                if not exc.recent_sip_denied or sip_end != now:
                    raise
                sip_end = now - _SIP_DELAY            # redo this chunk up to the allowed edge
                continue
            written += await self._store(self._write_chunk, symbol, bars, sip=True)
            cursor = end
        if sip_end == now:
            self._iex_since.pop(symbol, None)         # SIP now covers any earlier IEX tail
        else:
            tail_start = max(cursor, sip_end)
            bars = await self._pull(symbol, tail_start, now, "iex")
            self._iex_since[symbol] = int(tail_start.timestamp() * 1000)
            written += await self._store(self._write_chunk, symbol, bars)
        await self._store(self.store.set_state, symbol, status="ok", note="")
        return written

    def _reference_trading_day(self, now: datetime) -> date:
        """The trading day _SPLIT_LOOKBACK_DAYS trading days before today (ET)."""
        d = now.astimezone(ET).date()
        for _ in range(_SPLIT_LOOKBACK_DAYS):
            d -= timedelta(days=1)
            while not _is_trading_day(d):
                d -= timedelta(days=1)
        return d

    async def check_splits(self, symbol: str) -> bool:
        """Re-fetch a trading day ~5 trading days back; changed stored closes mean a split.
        Looking back several days (not just yesterday) means an adjustment Alpaca applies
        a day or more after the ex-date is still caught on a later morning's check."""
        day = self._reference_trading_day(self._now())
        open_ms = et_session_start_ms(day)
        close_ms = open_ms + 390 * _MINUTE_MS
        stored = {b["time"]: b["close"]
                  for b in await self._store(self.store.minutes, symbol, open_ms, close_ms)}
        if not stored:
            return False
        fetched = await self._pull(symbol, _from_ms(open_ms), _from_ms(close_ms), "sip")
        overlap = moved = 0
        for b in fetched:
            old = stored.get(b["time"])
            if old:
                overlap += 1
                if abs(b["close"] - old) / abs(old) > _SPLIT_TOLERANCE:
                    moved += 1
        # A real split moves (nearly) every bar; a stale IEX tail moves only a few.
        if overlap >= _SPLIT_MIN_OVERLAP and moved / overlap > 0.5:
            logger.warning("history: %s prices changed for %s; re-downloading", symbol, day)
            await self._store(self.store.delete_symbol, symbol)
            return True
        return False

    async def _missing_rth(self, symbol: str, day: date) -> list[int]:
        open_ms = et_session_start_ms(day)
        end_ms = open_ms + 390 * _MINUTE_MS
        have = {m["time"] for m in await self._store(self.store.minutes, symbol, open_ms, end_ms)}
        return [t for t in range(open_ms, end_ms, _MINUTE_MS) if t not in have]

    async def finalize_day(self, day: date) -> None:
        """Make sure `day`'s official RTH minutes are stored, then drop that day's trades and
        trade-built minutes. Official bars win: live data is never copied into bars_1m.
        A failed re-fetch leaves the day undeleted so the next pass retries it."""
        open_ms = et_session_start_ms(day)
        start, end = _from_ms(open_ms), _from_ms(open_ms + 390 * _MINUTE_MS)
        failed = False
        for symbol in stock_symbols():
            missing = await self._missing_rth(symbol, day)
            if not missing:
                continue
            try:
                bars = await self._pull(symbol, start, end, "sip")
                await self._store(self._write_chunk, symbol, bars, sip=True)
                missing = await self._missing_rth(symbol, day)
                note = (f"{len(missing)} minutes missing on {day.isoformat()}"
                        if missing else None)
            except Exception as exc:  # noqa: BLE001 - recorded; the day is retried next pass
                failed = True
                note = f"finalize {day.isoformat()} failed: {_short_reason(exc)}"
                logger.warning("history finalize %s %s failed: %s", symbol, day, note)
            if note:
                await self._store(self.store.set_state, symbol, note=note)
        if not failed:
            await self._store(self.store.delete_live_day, day)

    async def _finalize_due_days(self) -> None:
        local = self._now().astimezone(ET)
        today, after_close = local.date(), local.time() >= dtime(20, 15)
        for d in await self._store(self.store.live_days):
            if d < today or (d == today and after_close):
                try:
                    await self.finalize_day(d)
                except Exception:  # noqa: BLE001 - retried next pass
                    logger.exception("history: finalizing %s failed", d)

    async def run_once(self) -> None:
        self.errors = 0
        today = self._now().astimezone(ET).date().isoformat()
        for symbol in stock_symbols():
            try:
                state = await self._store(self.store.get_state, symbol) or {}
                if state.get("last_full_check") != today:
                    await self.check_splits(symbol)
                    await self._store(self.store.set_state, symbol, last_full_check=today)
                await self.sync_symbol(symbol)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest
                self.errors += 1
                note = _short_reason(exc)
                logger.warning("history sync %s failed: %s", symbol, note)
                try:
                    await self._store(self.store.set_state, symbol, status="error", note=note)
                except Exception:  # noqa: BLE001
                    logger.exception("history: could not record error state for %s", symbol)
        await self._finalize_due_days()


def next_delay(now: datetime) -> float:
    """Seconds to sleep after a pass finished at `now`."""
    local = now.astimezone(ET)
    t = local.time()
    trading = _is_trading_day(local.date())
    if trading and dtime(9, 30) <= t < dtime(16, 0):
        return 60.0
    if trading and dtime(4, 0) <= t < dtime(20, 15):
        return 300.0
    day = local.date() if (trading and t < dtime(4, 0)) else local.date() + timedelta(days=1)
    for _ in range(14):
        if _is_trading_day(day):
            break
        day += timedelta(days=1)
    target = datetime.combine(day, dtime(4, 0), tzinfo=ET)
    return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


async def start_sync() -> None:
    """Background entry point (lifespan task): open the store off the event loop, then run
    forever. A store that cannot be opened is logged and the task ends; never raises."""
    try:
        store = await asyncio.to_thread(get_store)
    except Exception:  # noqa: BLE001 - a bad history path must not affect the app
        logger.exception("history sync: could not open the history store; sync not started")
        return
    await run_forever(HistorySync(store))


async def run_forever(sync: HistorySync) -> None:
    try:
        lock = open(f"{sync.store.path}.lock", "a+")
    except OSError:
        logger.exception("history sync: cannot open %s.lock; sync not started", sync.store.path)
        return
    rec_task: asyncio.Task | None = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("history sync: another process holds %s.lock; standing down",
                        sync.store.path)
            return
        logger.info("history sync started")
        # The live recorder writes the same store, so it runs only under this lock.
        try:
            rec = recorder_mod.LiveRecorder(sync.store)
        except Exception:  # noqa: BLE001 - the sync must run even if the recorder cannot
            logger.exception("live recorder could not start; continuing without it")
        else:
            recorder_mod.recorder = rec
            rec_task = asyncio.create_task(rec.run(), name="history-live-recorder")
        failures = 0
        while True:
            try:
                await sync.run_once()
                failures = failures + 1 if sync.errors else 0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("history sync pass crashed")
                failures += 1
            delay = (min(_MAX_BACKOFF, 60.0 * 2 ** failures) if failures
                     else next_delay(datetime.now(UTC)))
            await asyncio.sleep(delay)
    finally:
        if rec_task is not None:
            rec_task.cancel()
            try:
                await rec_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("live recorder ended with an error")
            recorder_mod.recorder = None
        lock.close()


def history_status() -> dict:
    store = get_store()
    out: dict = {}
    for symbol in stock_symbols():
        lo, hi = store.bounds(symbol)
        state = store.get_state(symbol) or {}
        out[symbol] = {"earliest": lo, "latest": hi,
                       "status": state.get("status") or "pending",
                       "note": state.get("note") or ""}
    return out
