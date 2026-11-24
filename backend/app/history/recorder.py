"""Live trade recorder: DXLink TimeAndSale -> ticks + live_1m during US regular hours.

Listens through the app's shared streamer, buffers trades in memory and flushes about once
a second to the history store (off the event loop). Runs inside the history sync task, so
only the process holding the store lock records. Every failure is logged and retried; the
recorder never raises into the sync loop and never blocks the shared feed."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, time as dtime, timedelta, timezone

from tastytrade.dxfeed import TimeAndSale

from app.history.live_bars import ET, Trade, fold, is_rth_trade, stock_symbols
from app.history.store import HistoryStore
from app.services.market import _is_trading_day
from app.tastytrade.streamer import get_streamer

logger = logging.getLogger(__name__)

UTC = timezone.utc
_MINUTE_MS = 60_000
_WINDOW_OPEN = dtime(9, 29, 30)      # subscribe a little before the open
_WINDOW_CLOSE = dtime(16, 1, 0)      # 60 s grace for the delayed 16:00 closing print
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)
_IDLE_WAKE = 60.0                    # outside the window, re-check at least this often
_MAX_BACKOFF = 30.0
_ACCEPTED_TYPES = {"NEW", "CORRECTION"}
_BAR_KEYS = ("time", "open", "high", "low", "close", "volume")
_CLS = "TimeAndSale"


def _type_name(t) -> str:
    return str(getattr(t, "name", t)).upper()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _in_window(local: datetime) -> bool:
    return _is_trading_day(local.date()) and _WINDOW_OPEN <= local.time() < _WINDOW_CLOSE


def _seconds_to_window(local: datetime) -> float:
    day = local.date()
    if not (_is_trading_day(day) and local.time() < _WINDOW_OPEN):
        day += timedelta(days=1)
        for _ in range(14):
            if _is_trading_day(day):
                break
            day += timedelta(days=1)
    target = datetime.combine(day, _WINDOW_OPEN, tzinfo=ET)
    return (target - local).total_seconds()


class LiveRecorder:
    def __init__(self, store: HistoryStore, streamer_factory=get_streamer,
                 now=lambda: datetime.now(UTC), flush_seconds: float = 1.0,
                 subscribe_timeout: float = 15.0, max_buffer: int = 2_000_000):
        self.store = store
        self._factory = streamer_factory
        self._now = now
        self._flush_seconds = flush_seconds
        self._subscribe_timeout = subscribe_timeout
        self._max_buffer = max_buffer
        self._symbols: frozenset[str] = frozenset(stock_symbols())
        self._buffer: list[Trade] = []
        self._bars: dict[str, dict[int, dict]] = {}
        self._streamer = None                  # the instance our listener is attached to
        self._seeded_day: date | None = None
        self._bars_day: date | None = None
        self._seed_failures = 0
        self._flush_failures = 0

    # --- hot path (runs inside the streamer pump; no I/O, no awaits) ------------------
    def on_event(self, ev) -> None:
        if _type_name(ev.type) not in _ACCEPTED_TYPES:
            return
        symbol = ev.event_symbol
        if symbol not in self._symbols:
            return
        ts = int(ev.time)
        if not is_rth_trade(ts, ev.extended_trading_hours):
            return
        price = float(ev.price)
        if not math.isfinite(price) or price <= 0:
            return
        tr = Trade(symbol, ts, int(ev.index), price, float(ev.size or 0), bool(ev.valid_tick))
        self._buffer.append(tr)
        fold(self._bars.setdefault(symbol, {}), [tr])

    # --- store -------------------------------------------------------------------------
    async def flush(self) -> int:
        batch, self._buffer = self._buffer, []
        if batch:
            try:
                await asyncio.to_thread(self.store.insert_trades, batch)
            except asyncio.CancelledError:
                self._buffer = batch + self._buffer   # the insert dedupes if it did land
                raise
            except Exception:  # noqa: BLE001
                self._buffer = batch + self._buffer
                over = len(self._buffer) - self._max_buffer
                if over > 0:
                    del self._buffer[:over]
                    logger.error("live recorder: buffer over its %d-trade cap; dropped the "
                                 "%d oldest trades", self._max_buffer, over)
                self._flush_failures += 1
                logger.warning("live recorder: flush of %d trades failed (%d in a row); "
                               "will retry", len(batch), self._flush_failures,
                               exc_info=self._flush_failures == 1)
                return 0
            self._flush_failures = 0
        self._prune()
        return len(batch)

    def _current_minute(self) -> int:
        t = _ms(self._now())
        return t - t % _MINUTE_MS

    def _prune(self) -> None:
        keep_from = self._current_minute() - _MINUTE_MS
        for bars in self._bars.values():
            for m in [m for m in bars if m < keep_from]:
                del bars[m]

    def live_bars(self, symbol: str) -> list[dict]:
        """The just-closed and the forming minute for today, from memory (0-2 bars)."""
        local = self._now().astimezone(ET)
        if not (_is_trading_day(local.date()) and _RTH_OPEN <= local.time() < _RTH_CLOSE):
            return []
        cur = self._current_minute()
        bars = self._bars.get(symbol, {})
        return [{k: bars[m][k] for k in _BAR_KEYS}
                for m in (cur - _MINUTE_MS, cur) if m in bars]

    def _load_seed(self, day: date, cur: int) -> dict[str, list[dict]]:
        """Thread body: rebuild today's live bars from ticks, read the last two minutes."""
        self.store.rebuild_live(day)
        return {s: self.store.live_minutes(s, cur - _MINUTE_MS, cur + _MINUTE_MS)
                for s in self._symbols}

    async def _seed(self, day: date) -> None:
        seed = await asyncio.to_thread(self._load_seed, day, self._current_minute())
        for symbol, rows in seed.items():
            bars = self._bars.setdefault(symbol, {})
            for row in rows:
                bars.setdefault(row["time"], dict(row))
        self._seeded_day = day

    # --- feed --------------------------------------------------------------------------
    def _attach(self, streamer) -> None:
        if streamer is not self._streamer:
            self._detach()
            streamer.add_listener(_CLS, self.on_event)
            self._streamer = streamer

    def _detach(self) -> None:
        if self._streamer is not None:
            try:
                self._streamer.remove_listener(_CLS, self.on_event)
            except Exception:  # noqa: BLE001
                logger.warning("live recorder: could not remove listener", exc_info=True)
            self._streamer = None

    async def _ensure_feed(self) -> bool:
        streamer = self._factory()
        self._attach(streamer)
        alive = getattr(streamer, "_alive", None)
        if not (alive is not None and alive()) and not await streamer._ensure():
            return False
        for symbol in sorted(self._symbols):
            # Re-read every time: a DXLink drop replaces the whole _subscribed dict.
            if symbol not in streamer._subscribed.get(_CLS, ()):
                # Backstop above the SDK's own 10 s channel wait (held under the shared
                # _sub_lock). Cancelling can leave the SDK's channel state half-open, so
                # on expiry reset the shared connection, like a failed subscribe does.
                try:
                    ok = await asyncio.wait_for(streamer._subscribe(TimeAndSale, symbol),
                                                self._subscribe_timeout)
                except asyncio.TimeoutError:
                    await self._teardown(streamer)
                    raise ConnectionError(f"TimeAndSale subscribe {symbol} timed out after "
                                          f"{self._subscribe_timeout:g}s") from None
                if not ok:
                    return False
        return True

    @staticmethod
    async def _teardown(streamer) -> None:
        """Best effort: drop the shared connection so the next try starts clean."""
        try:
            await streamer.teardown()
        except Exception:  # noqa: BLE001 - never raise from the reset itself
            logger.warning("live recorder: streamer teardown after a subscribe timeout "
                           "failed", exc_info=True)

    async def _release_feed(self) -> None:
        streamer = self._streamer
        if streamer is None:
            return
        held = sorted(streamer._subscribed.get(_CLS, ()))
        if held:
            await streamer._unsubscribe(TimeAndSale, held)

    # --- loop --------------------------------------------------------------------------
    async def _step(self) -> float:
        """One iteration; returns how long to sleep before the next one."""
        self._symbols = frozenset(stock_symbols())
        local = self._now().astimezone(ET)
        if not _in_window(local):
            await self._release_feed()
            await self.flush()
            return min(_IDLE_WAKE, max(self._flush_seconds, _seconds_to_window(local)))
        if self._bars_day != local.date():
            self._bars.clear()                   # a new session
            self._bars_day = local.date()
        if self._seeded_day != local.date():
            try:
                await self._seed(local.date())
                self._seed_failures = 0
            except Exception:  # noqa: BLE001 - record unseeded; retry next iteration
                self._seed_failures += 1
                if self._seed_failures == 1 or self._seed_failures % 60 == 0:
                    logger.warning("live recorder: seeding live bars failed (%d in a row); "
                                   "recording unseeded and retrying", self._seed_failures,
                                   exc_info=self._seed_failures == 1)
        connected = await self._ensure_feed()
        await self.flush()
        if not connected:
            raise ConnectionError("DXLink not connected; TimeAndSale not subscribed")
        return self._flush_seconds

    async def run(self) -> None:
        logger.info("live recorder started for %s", ", ".join(sorted(self._symbols)))
        failures = 0
        try:
            while True:
                try:
                    delay = await self._step()
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never kill the sync loop
                    failures += 1
                    delay = min(_MAX_BACKOFF, self._flush_seconds * 2 ** min(failures, 16))
                    if isinstance(exc, ConnectionError):
                        logger.warning("live recorder: %s; retrying in %.1fs", exc, delay)
                    else:
                        logger.exception("live recorder iteration failed; retrying in %.1fs",
                                         delay)
                await asyncio.sleep(delay)
        finally:
            self._detach()


recorder: LiveRecorder | None = None
