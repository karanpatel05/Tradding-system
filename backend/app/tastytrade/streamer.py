"""One shared DXLink websocket for the whole app. Quote/Trade events are pumped into
in-memory caches (subscribe once per symbol, read many); Candle events serve history.
All failures degrade to None/[] - the facade decides about fallback. DXFeed keeps
serving last-known quotes after hours, which is the point of this migration."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import anyio
from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle, Quote, TimeAndSale, Trade

from app.tastytrade.session import get_tt_session

logger = logging.getLogger("trading.tt_streamer")


def _accept_fractional_trade_sizes() -> None:
    """SDK 12.4.1 types Trade.size/day_volume as int, but Tastytrade sends fractional
    values (e.g. dayVolume 19189452.77). The SDK silently skips events that fail
    validation, so EVERY live Trade was dropped and 'last' froze at the previous
    session's trade. Relax both fields to float. Remove once the SDK is fixed."""
    changed = False
    for name in ("size", "day_volume"):
        field = Trade.model_fields.get(name)
        if field is not None and field.annotation == (int | None):
            field.annotation = float | None
            changed = True
    if changed:
        Trade.model_rebuild(force=True)
    # Some trades lack a contemporaneous bid, ask or sale-condition string.
    # The SDK otherwise discards the entire print, including its price and size.
    if TimeAndSale.model_fields["size"].annotation is int:
        TimeAndSale.model_fields["size"].annotation = float | None
        for name in ("bid_price", "ask_price", "exchange_sale_conditions"):
            field = TimeAndSale.model_fields[name]
            field.annotation = field.annotation | None
        TimeAndSale.model_rebuild(force=True)


_accept_fractional_trade_sizes()

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1d": "1d"}


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


class TickSubscription:
    """One browser's ordered buffer. A gap ends this subscription."""

    QUEUE_LIMIT = 4096
    BATCH_LIMIT = 256

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue(self.QUEUE_LIMIT)
        self.gapped = False

    def publish(self, tick: dict) -> None:
        if self.gapped:
            return
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            self.invalidate()

    def invalidate(self) -> None:
        if not self.gapped:
            self.gapped = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(None)

    async def next_batch(self) -> list[dict]:
        first = await self._queue.get()
        if first is None:
            return []
        batch = [first]
        while len(batch) < self.BATCH_LIMIT and not self._queue.empty():
            batch.append(self._queue.get_nowait())
        return batch


class TTStreamer:
    def __init__(self) -> None:
        self._dx = None
        self._lock = asyncio.Lock()
        self._candle_lock = asyncio.Lock()
        self._pumps: list[asyncio.Task] = []
        self._quotes: dict[str, dict] = {}
        self._trades: dict[str, float] = {}
        # Wall-clock receive time (epoch seconds) of the latest Quote/Trade per symbol,
        # so consumers can enforce a max quote age. Kept across reconnects on purpose:
        # a dropped feed shows up as an ageing timestamp, never as a fresh-looking value.
        self._quote_ts: dict[str, float] = {}
        self._trade_ts: dict[str, float] = {}
        # Previous session close per symbol, from Trade.change (price - change).
        self._prev_close: dict[str, float] = {}
        self._candles: dict[str, dict[int, dict]] = {}
        self._subscribed: dict[str, set[str]] = {"Quote": set(), "Trade": set()}
        self._tick_listeners: dict[str, set[TickSubscription]] = {}
        self._runner: asyncio.Task | None = None
        # SDK 12.x race: a channel is marked ready on CHANNEL_OPENED, BEFORE its
        # FEED_SETUP is sent, so a concurrent 2nd subscribe can go out first; the server
        # then answers in FULL format and the SDK reader dies (KeyError: 0). Subscribes
        # are rare (once per symbol), so serialize them all.
        self._sub_lock = asyncio.Lock()

    # --- connection -----------------------------------------------------
    # The SDK runs its websocket reader + heartbeat in an anyio task group bound to the
    # task that ENTERS its context. If a request handler entered it, a dropped socket
    # raised inside that (long-gone) task's scope: our pumps blocked forever on empty
    # queues, _dx stayed set, every new subscribe failed, and quotes stayed null until
    # a restart. So one dedicated runner task owns the connection for its whole life:
    # when the reader dies, the error surfaces HERE and the connection is dropped, so
    # the next read reconnects.
    _CONNECT_TIMEOUT = 20.0

    async def _connect(self) -> bool:
        session = await get_tt_session()
        if session is None:
            return False
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        runner = asyncio.create_task(self._run(session, ready), name="tt-dxlink")
        self._runner = runner
        try:
            return await asyncio.wait_for(asyncio.shield(ready), self._CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("DXLink connect timed out")
            runner.cancel()
            return False

    async def _run(self, session, ready: asyncio.Future) -> None:
        me = asyncio.current_task()
        try:
            async with DXLinkStreamer(session) as dx:
                self._dx = dx
                self._start_pumps()
                ready.set_result(True)
                await asyncio.Event().wait()        # until cancelled or the reader dies
        except asyncio.CancelledError:
            pass                                    # teardown() asked us to stop
        except BaseException:  # noqa: BLE001 - incl. anyio ExceptionGroups
            if ready.done():
                logger.warning("DXLink connection dropped; reconnecting on next read",
                               exc_info=True)
            else:
                logger.warning("DXLink connect failed", exc_info=True)
        finally:
            if not ready.done():
                ready.set_result(False)
            if self._runner is me:                  # not already replaced
                self._drop_state()

    def _drop_state(self) -> None:
        for t in self._pumps:
            t.cancel()
        self._pumps = []
        self._dx = None
        self._runner = None
        self._subscribed = {"Quote": set(), "Trade": set()}
        for listeners in self._tick_listeners.values():
            for listener in listeners:
                listener.invalidate()
        self._tick_listeners.clear()

    def _alive(self) -> bool:
        return self._dx is not None and (self._runner is None or not self._runner.done())

    async def _ensure(self) -> bool:
        if self._alive():
            return True
        async with self._lock:
            if self._alive():
                return True
            return await self._connect()

    async def _subscribe(self, cls, symbol: str) -> bool:
        """Subscribe once per symbol. A failure means the socket is dead: drop the
        connection so the next read reconnects instead of failing forever."""
        if symbol in self._subscribed[cls.__name__]:
            return True
        async with self._sub_lock:
            if symbol in self._subscribed[cls.__name__]:
                return True
            dx = self._dx
            if dx is None:
                return False
            try:
                await dx.subscribe(cls, [symbol])
            except Exception:  # noqa: BLE001
                logger.warning("DXLink subscribe %s %s failed; dropping connection",
                               cls.__name__, symbol, exc_info=True)
                await self.teardown()
                return False
            self._subscribed[cls.__name__].add(symbol)
            return True

    def _start_pumps(self) -> None:
        self._pumps = [
            asyncio.create_task(self._pump(Quote)),
            asyncio.create_task(self._pump(Trade)),
            asyncio.create_task(self._pump(TimeAndSale)),
            asyncio.create_task(self._pump(Candle)),
        ]

    async def _pump(self, cls) -> None:
        try:
            async for ev in self._dx.listen(cls):
                if cls is Quote:
                    self._quotes[ev.event_symbol] = {
                        "bid": _num(ev.bid_price), "ask": _num(ev.ask_price)}
                    self._quote_ts[ev.event_symbol] = time.time()
                elif cls is Trade:
                    p = _num(ev.price)
                    if p is not None:
                        self._trades[ev.event_symbol] = p
                        self._trade_ts[ev.event_symbol] = time.time()
                        # dxfeed Trade.change is vs the previous session's close.
                        ch = _num(getattr(ev, "change", None))
                        if ch is not None and p - ch > 0:
                            self._prev_close[ev.event_symbol] = round(p - ch, 4)
                elif cls is TimeAndSale:
                    self._publish_sale(ev)
                else:  # Candle: event_symbol like "QQQ{=5m}"
                    bucket = self._candles.setdefault(ev.event_symbol, {})
                    t = int(ev.time)
                    bucket[t] = {"time": t, "open": _num(ev.open), "high": _num(ev.high),
                                 "low": _num(ev.low), "close": _num(ev.close),
                                 "volume": _num(ev.volume) or 0.0}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - websocket died; drop so next call reconnects
            logger.warning("DXLink pump (%s) died; will reconnect on demand",
                           cls.__name__, exc_info=True)
            await self.teardown()

    def _publish_sale(self, ev: TimeAndSale) -> None:
        listeners = self._tick_listeners.get(ev.event_symbol)
        if (not listeners or ev.type != "NEW" or ev.extended_trading_hours
                or ev.time <= 0 or ev.size is None):
            return
        price, size = float(ev.price), float(ev.size)
        if not (math.isfinite(price) and price > 0 and math.isfinite(size) and size > 0):
            return
        tick = {"symbol": ev.event_symbol, "time": ev.time, "price": price,
                "size": size, "updatesPrice": ev.valid_tick}
        # The closing auction belongs to the final regular-session candle.
        # Preserve its exchange timestamp on the tape, including on half days.
        if ev.time % 3_600_000 == 0:
            from app.strategies.xsp_iron_fly import calendar
            at = datetime.fromtimestamp(ev.time / 1000, calendar.ET)
            close = calendar.EARLY_CLOSE if calendar.is_early_close(at.date()) else calendar.REGULAR_CLOSE
            if at.time() == close:
                tick["barTime"] = ev.time - 1
        for listener in listeners:
            listener.publish(tick)

    async def _add_tick_listener(self, symbols: list[str], listener: TickSubscription) -> bool:
        if not await self._ensure():
            return False
        async with self._sub_lock:
            dx = self._dx
            if dx is None:
                return False
            new = [symbol for symbol in symbols if symbol not in self._tick_listeners]
            for symbol in symbols:
                self._tick_listeners.setdefault(symbol, set()).add(listener)
            if new:
                try:
                    await dx.subscribe(TimeAndSale, new, refresh_interval=0)
                except Exception:
                    logger.warning("DXLink tick subscription failed", exc_info=True)
                    if self._dx is dx:
                        await self.teardown()
                    return False
            return self._dx is dx

    async def _remove_tick_listener(self, symbols: list[str], listener: TickSubscription):
        async with self._sub_lock:
            unused = []
            for symbol in symbols:
                listeners = self._tick_listeners.get(symbol)
                if listeners and listener in listeners:
                    listeners.remove(listener)
                    if not listeners:
                        unused.append(symbol)
                        del self._tick_listeners[symbol]
            dx = self._dx
            if unused and dx is not None:
                try:
                    await dx.unsubscribe(TimeAndSale, unused)
                except Exception:
                    logger.warning("DXLink tick unsubscribe failed", exc_info=True)
                    if self._dx is dx:
                        await self.teardown()

    @asynccontextmanager
    async def listen_ticks(self, symbols: list[str]):
        """Subscribe once per symbol across viewers. Feed loss wakes every listener."""
        listener = TickSubscription()
        try:
            if not await self._add_tick_listener(symbols, listener):
                listener.invalidate()
            yield listener
        finally:
            with anyio.CancelScope(shield=True):
                await self._remove_tick_listener(symbols, listener)

    async def teardown(self) -> None:
        runner = self._runner
        if runner is not None and runner is not asyncio.current_task():
            runner.cancel()                         # its own task exits the SDK context
            try:
                await runner
            except asyncio.CancelledError:
                # The runner swallows its own cancel, so this is OUR caller being
                # cancelled (e.g. a wait_for timeout): finish cleanup, then honour it.
                self._drop_state()
                raise
            except Exception:  # noqa: BLE001
                pass
        dx = self._dx
        self._drop_state()
        if runner is None and dx is not None and hasattr(dx, "__aexit__"):
            try:                                    # context entered outside a runner
                await dx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # --- reads ------------------------------------------------------------
    def prev_close(self, symbol: str) -> float | None:
        """Previous session close (from the latest Trade), or None. Sync cache read."""
        return self._prev_close.get(symbol)

    def cached_quote(self, symbol: str) -> tuple[dict | None, float | None]:
        """Latest cached Quote and its receive time (epoch s), without waiting or
        subscribing. For fast monitor loops; call ``quote()`` once to subscribe."""
        return self._quotes.get(symbol), self._quote_ts.get(symbol)

    def cached_trade(self, symbol: str) -> tuple[float | None, float | None]:
        """Latest cached Trade price and its receive time (epoch s), non-blocking."""
        return self._trades.get(symbol), self._trade_ts.get(symbol)

    async def _wait_for(self, cache: dict, symbol: str, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = cache.get(symbol)
            if v is not None:
                return v
            await asyncio.sleep(0.1)
        return cache.get(symbol)

    async def quote(self, symbol: str, timeout: float = 3.0) -> dict | None:
        if not await self._ensure() or not await self._subscribe(Quote, symbol):
            return None
        q = await self._wait_for(self._quotes, symbol, timeout)
        return q if q and (q["bid"] is not None or q["ask"] is not None) else None

    async def trade_price(self, symbol: str, timeout: float = 3.0) -> float | None:
        if not await self._ensure() or not await self._subscribe(Trade, symbol):
            return None
        return await self._wait_for(self._trades, symbol, timeout)

    async def candles(self, symbol: str, interval: str,
                      start: datetime, end: datetime | None = None,
                      idle_seconds: float = 2.0,
                      first_event_timeout: float = 10.0,
                      max_seconds: float = 30.0) -> list[dict]:
        """Collect Candle history: subscribe from `start`, wait for the first event (up to
        `first_event_timeout`; right after a reconnect it can lag well past two idle
        periods), then until no new events arrive for `idle_seconds`, unsubscribe, and
        return bars in [start, end] oldest-first. Collection never runs longer than
        `max_seconds` once it holds the lock, so callers need no outer timeout."""
        dx_interval = _INTERVALS.get(interval)
        if dx_interval is None or not await self._ensure():
            return []
        # dxfeed NORMALIZES the echoed candle symbol: subscribe "QQQ{=1d}" and events
        # arrive as "QQQ{=d,tho=true}" (canonical interval + trading-hours flag). Match
        # buckets by PREFIX, never by the exact subscription string.
        prefix = f"{symbol}{{="

        def _bucket_count() -> int:
            return sum(len(b) for k, b in self._candles.items() if k.startswith(prefix))

        async with self._candle_lock:            # one candle collection at a time
            for k in [k for k in self._candles if k.startswith(prefix)]:
                del self._candles[k]
            async with self._sub_lock:              # same channel-setup race as above
                await self._dx.subscribe_candle([symbol], dx_interval, start_time=start)
            loop = asyncio.get_running_loop()
            hard_stop = loop.time() + max_seconds
            deadline = min(loop.time() + first_event_timeout, hard_stop)
            while _bucket_count() == 0 and loop.time() < deadline:
                await asyncio.sleep(min(idle_seconds, 0.25))
            seen = -1
            while loop.time() < hard_stop:
                await asyncio.sleep(idle_seconds)
                n = _bucket_count()
                if n == seen:
                    break
                seen = n
            try:
                await self._dx.unsubscribe_candle([symbol], dx_interval)
            except Exception:  # noqa: BLE001
                pass
            merged: dict[int, dict] = {}
            for k in [k for k in self._candles if k.startswith(prefix)]:
                merged.update(self._candles.pop(k))
            bars = sorted(merged.values(), key=lambda b: b["time"])
        lo = int(start.timestamp() * 1000)
        hi = int((end or datetime.now(timezone.utc)).timestamp() * 1000)
        # close must be a POSITIVE number: a still-forming candle can arrive with 0.0.
        return [{**b, "symbol": symbol} for b in bars
                if lo <= b["time"] <= hi and (b["close"] or 0) > 0]


_streamer: TTStreamer | None = None


def get_streamer() -> TTStreamer:
    global _streamer
    if _streamer is None:
        _streamer = TTStreamer()
    return _streamer
