"""Background job that keeps the local 1-minute store current from Alpaca (live service only).

Resumes from the last stored minute, so a restart mid-backfill loses nothing. Only the process
holding `<db>.lock` runs it. Errors are recorded per symbol and never raised."""
from __future__ import annotations

import asyncio
import fcntl
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.history.aggregate import daily_from_minutes, et_session_start_ms
from app.history.alpaca_bars import AlpacaDataError, RateLimiter, fetch_minutes
from app.history.store import HistoryStore, get_store
from app.services.market import _is_trading_day

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
STOCK_SYMBOLS = ("SPY", "QQQ", "TSLA", "NVDA")
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

    def _write_chunk(self, symbol: str, bars: list[dict]) -> int:
        """Upsert a batch and rebuild the daily rows of every ET date it touched."""
        bars = [b for b in bars
                if dtime(4, 0) <= datetime.fromtimestamp(b["time"] / 1000, ET).time()
                < dtime(20, 0)]
        n = self.store.upsert_minutes(symbol, bars)
        days = {datetime.fromtimestamp(b["time"] / 1000, ET).date() for b in bars}
        rows: list[dict] = []
        for day in sorted(days):
            lo, hi = _day_range_ms(day)
            rows += daily_from_minutes(self.store.minutes(symbol, lo, hi))
        self.store.upsert_daily(symbol, rows)
        return n

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
            written += await self._store(self._write_chunk, symbol, bars)
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

    async def run_once(self) -> None:
        self.errors = 0
        today = self._now().astimezone(ET).date().isoformat()
        for symbol in STOCK_SYMBOLS:
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
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("history sync: another process holds %s.lock; standing down",
                        sync.store.path)
            return
        logger.info("history sync started")
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
        lock.close()


def history_status() -> dict:
    store = get_store()
    out: dict = {}
    for symbol in STOCK_SYMBOLS:
        lo, hi = store.bounds(symbol)
        state = store.get_state(symbol) or {}
        out[symbol] = {"earliest": lo, "latest": hi,
                       "status": state.get("status") or "pending",
                       "note": state.get("note") or ""}
    return out
