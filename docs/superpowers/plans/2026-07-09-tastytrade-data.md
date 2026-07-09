# Tastytrade Primary Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tastytrade (REST + DXLink streaming) becomes the primary market-data source for live quotes, option chains/mids, VIX, and historical candles; IBKR stays for order execution and as a data fallback while its Gateway is READY.

**Architecture:** New `app/tastytrade/` package (OAuth session + one shared DXLink websocket with quote/trade/candle caches) behind a provider-agnostic facade `app/marketdata/facade.py` that all consumers call. Each facade op tries Tastytrade → IBKR-when-READY → None. A mid sanity guard (crossed / wide-spread / jump rules) protects both sources. The TC live loop gains monitor-only mode (valuations without a broker; order actions require Gateway READY).

**Tech Stack:** Python 3.14, FastAPI, `tastytrade==12.4.1` (async-native SDK, already in the venv), ib_async (frozen fallback), pytest + fakes (no network in tests).

**Spec:** `docs/superpowers/specs/2026-07-09-tastytrade-data-design.md`

## Global Constraints

- Credentials are **OAuth**: `tt_provider_secret` / `tt_refresh_token` (env `TT_PROVIDER_SECRET` / `TT_REFRESH_TOKEN`). Never username/password.
- `data_primary` setting: `"tastytrade"` (default) or `"ibkr"`, reverses the facade's try order per call.
- IBKR fallback is attempted **only when the Gateway state is READY**.
- Facade ops return **None** (or `[]`) on total failure — they never raise into strategy loops.
- Mid sanity guard defaults (exact values): `sanity_rel_spread = 0.35`, `sanity_abs_spread = 2.50`, `sanity_max_jump_pct = 0.50`, `sanity_jump_window_seconds = 900`. A rejected mid is **None, never a number**; rejections are journaled as warnings; the jump memory stores **accepted** mids only.
- Journal a source event only on **transition** (serving source changes), not per call.
- Monitor-only mode: while market open and Gateway NOT READY, the TC loop updates position valuations but **never calls `manage_step`** (no enter/roll/adjust/exit); transition journaled once per state change.
- Order execution / reconciliation / kill-switch paths untouched. Builder still transmits no orders.
- No IBKR data code deleted — extraction/refactor only.
- Existing tests must keep passing, incl. standalone TC live tests (`test_tc_multi_live`, `test_tc_vix_live`, `test_tc_contracts_live`, `test_tc_roll_live`, `test_tc_live_persist`).
- Backend tests run as: `cd backend && rm -f _t_X.db && DATABASE_URL=sqlite+aiosqlite:///./_t_X.db ../.venv/bin/python -m pytest <files> -q; rm -f _t_X.db`.

## File Structure

```
backend/app/tastytrade/__init__.py        (new, empty)
backend/app/tastytrade/session.py         (new) OAuth session cache
backend/app/tastytrade/streamer.py        (new) DXLink quotes/trades/candles
backend/app/marketdata/__init__.py        (new, empty)
backend/app/marketdata/sanity.py          (new) MidSanity guard
backend/app/marketdata/facade.py          (new) stock_quote(s)/chain/option_mid/vix/bars
backend/app/ibkr/options_data.py          (new) IBKR helpers extracted from live_source
backend/app/providers/tastytrade_historical.py (new) HistoricalDataProvider via candles
backend/app/config.py                     (modify) new settings
backend/requirements.txt                  (modify) pin tastytrade
backend/app/strategies/triple_calendar/live_source.py   (modify) delegate to facade
backend/app/strategies/triple_calendar/strategy_class.py (modify) monitor-only mode
backend/app/services/market.py            (modify) overview via facade
backend/app/routers/strategies.py         (modify) position live quote via facade
backend/app/options/chain.py              (modify) spot/expiries via facade
backend/app/providers/__init__.py         (modify) factory prefers Tastytrade
backend/app/auth/routes.py                (modify) GET/PUT /tastytrade creds
frontend/src/api.js                       (modify) ttStatus/ttSave
frontend/src/components/ApiTab.jsx        (modify) Tastytrade card
```

---

### Task 1: Config, requirements, and the Tastytrade session manager

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/config.py` (inside `class Settings`)
- Create: `backend/app/tastytrade/__init__.py`, `backend/app/tastytrade/session.py`
- Test: `backend/tests/test_tt_session.py`

**Interfaces:**
- Produces: `Settings.tt_provider_secret: str`, `Settings.tt_refresh_token: str`, `Settings.data_primary: str`, `Settings.sanity_rel_spread: float`, `Settings.sanity_abs_spread: float`, `Settings.sanity_max_jump_pct: float`, `Settings.sanity_jump_window_seconds: int`; `app.tastytrade.session.get_tt_session() -> Session | None` (async), `reset_tt_session() -> None`, `tt_configured() -> bool`.

- [ ] **Step 1: Pin the SDK**

In `backend/requirements.txt`, after the `ib_async==2.1.0` line, add:
```
tastytrade==12.4.1
```
(It is already installed in `.venv`; the pin just records it.)

- [ ] **Step 2: Add settings**

In `backend/app/config.py`, inside `class Settings`, after the `ibkr_market_data_type` line, add:
```python
    # --- Tastytrade market data (OAuth personal grant; my.tastytrade.com → API access) ---
    tt_provider_secret: str = ""     # env TT_PROVIDER_SECRET
    tt_refresh_token: str = ""       # env TT_REFRESH_TOKEN
    data_primary: str = "tastytrade"  # "tastytrade" | "ibkr" — facade try order
    # Mid sanity guard: reject crossed/wide/jumpy option mids (see marketdata/sanity.py).
    sanity_rel_spread: float = 0.35
    sanity_abs_spread: float = 2.50
    sanity_max_jump_pct: float = 0.50
    sanity_jump_window_seconds: int = 900
```

- [ ] **Step 3: Write the failing test**

```python
# backend/tests/test_tt_session.py
"""Session manager: no creds -> None (unavailable); creds -> constructs the SDK Session
lazily and caches it; reset clears the cache. The SDK Session is faked - no network."""
import pytest

from app.config import get_settings
from app.tastytrade import session as tt_session


class _FakeSession:
    def __init__(self, provider_secret=None, refresh_token=None, is_test=False):
        self.provider_secret = provider_secret
        self.refresh_token = refresh_token


@pytest.mark.asyncio
async def test_no_creds_returns_none(monkeypatch):
    monkeypatch.setattr(get_settings(), "tt_provider_secret", "")
    monkeypatch.setattr(get_settings(), "tt_refresh_token", "")
    tt_session.reset_tt_session()
    assert await tt_session.get_tt_session() is None
    assert tt_session.tt_configured() is False


@pytest.mark.asyncio
async def test_creds_construct_and_cache(monkeypatch):
    monkeypatch.setattr(get_settings(), "tt_provider_secret", "sec")
    monkeypatch.setattr(get_settings(), "tt_refresh_token", "ref")
    monkeypatch.setattr(tt_session, "Session", _FakeSession)
    tt_session.reset_tt_session()
    s1 = await tt_session.get_tt_session()
    s2 = await tt_session.get_tt_session()
    assert s1 is s2 and s1.provider_secret == "sec" and s1.refresh_token == "ref"
    assert tt_session.tt_configured() is True
    tt_session.reset_tt_session()
    assert (await tt_session.get_tt_session()) is not s1
```

- [ ] **Step 4: Run to verify it fails**

Run: `cd backend && rm -f _t_tts.db && DATABASE_URL=sqlite+aiosqlite:///./_t_tts.db ../.venv/bin/python -m pytest tests/test_tt_session.py -q; rm -f _t_tts.db`
Expected: FAIL — `ModuleNotFoundError: app.tastytrade`.

- [ ] **Step 5: Implement**

`backend/app/tastytrade/__init__.py`: empty file.

```python
# backend/app/tastytrade/session.py
"""Cached Tastytrade OAuth session. The SDK's Session fetches and auto-refreshes its
short-lived access token internally, so construction is cheap and there is no daily
login. Missing/blank creds -> None; the facade treats that as 'Tastytrade unavailable'."""
from __future__ import annotations

import asyncio

from tastytrade import Session

from app.config import get_settings

_session: Session | None = None
_lock = asyncio.Lock()


def tt_configured() -> bool:
    s = get_settings()
    return bool(s.tt_provider_secret and s.tt_refresh_token)


async def get_tt_session() -> Session | None:
    global _session
    if not tt_configured():
        return None
    if _session is not None:
        return _session
    async with _lock:
        if _session is None:
            s = get_settings()
            _session = Session(provider_secret=s.tt_provider_secret,
                               refresh_token=s.tt_refresh_token)
        return _session


def reset_tt_session() -> None:
    """Drop the cached session (call after credentials change)."""
    global _session
    _session = None
```

- [ ] **Step 6: Run to verify it passes**

Same command as Step 4. Expected: `2 passed`. Also: `../.venv/bin/python -c "import app.main; print('main imports OK')"`.

- [ ] **Step 7: Commit**

```bash
git add backend/requirements.txt backend/app/config.py backend/app/tastytrade backend/tests/test_tt_session.py
git commit -m "feat(tt): settings + cached Tastytrade OAuth session manager"
```

---

### Task 2: DXLink streamer manager (quotes, trades, candles)

**Files:**
- Create: `backend/app/tastytrade/streamer.py`
- Test: `backend/tests/test_tt_streamer.py`

**Interfaces:**
- Consumes: `get_tt_session()` (Task 1).
- Produces: `get_streamer() -> TTStreamer` (module singleton) with
  `async quote(symbol: str, timeout: float = 3.0) -> dict | None` (`{"bid": float|None, "ask": float|None}`),
  `async trade_price(symbol: str, timeout: float = 3.0) -> float | None`,
  `async candles(symbol: str, interval: str, start: datetime, end: datetime | None = None) -> list[dict]` (Bar shape: `{"time": int ms, "open","high","low","close","volume": float, "symbol": str}`),
  `async teardown() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tt_streamer.py
"""TTStreamer against a fake DXLink streamer: subscribe-on-demand, cached quote reads,
trade last-price, candle collection into normalized Bars. No network."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.tastytrade import streamer as st_mod


class _FakeDx:
    """Stands in for DXLinkStreamer: events are pushed by the test into per-type queues."""
    def __init__(self):
        self.queues = {}
        self.subscribed = []

    def _q(self, cls):
        return self.queues.setdefault(cls.__name__, asyncio.Queue())

    async def subscribe(self, cls, symbols):
        self.subscribed.append((cls.__name__, list(symbols)))

    async def subscribe_candle(self, symbols, interval, start_time=None, extended_trading_hours=False):
        self.subscribed.append(("Candle", list(symbols), interval))

    async def unsubscribe_candle(self, symbols, interval):
        pass

    async def listen(self, cls):
        q = self._q(cls)
        while True:
            yield await q.get()

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_quote_and_trade_from_cache(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Quote, Trade
    task = asyncio.create_task(_push_later(fake, Quote, SimpleNamespace(
        event_symbol="QQQ", bid_price=500.0, ask_price=500.2)))
    q = await s.quote("QQQ", timeout=2.0)
    assert q == {"bid": 500.0, "ask": 500.2}
    assert ("Quote", ["QQQ"]) in fake.subscribed
    task2 = asyncio.create_task(_push_later(fake, Trade, SimpleNamespace(
        event_symbol="QQQ", price=500.1)))
    assert await s.trade_price("QQQ", timeout=2.0) == 500.1
    await task; await task2; await s.teardown()


@pytest.mark.asyncio
async def test_candles_collected_and_normalized(monkeypatch):
    fake = _FakeDx()
    s = st_mod.TTStreamer()
    monkeypatch.setattr(s, "_connect", _fake_connect(fake, s))
    from tastytrade.dxfeed import Candle
    t0 = datetime(2026, 7, 8, 14, 30, tzinfo=timezone.utc)
    ms = int(t0.timestamp() * 1000)
    ev = SimpleNamespace(event_symbol="QQQ{=5m}", time=ms, open=1.0, high=2.0,
                         low=0.5, close=1.5, volume=100)
    task = asyncio.create_task(_push_later(fake, Candle, ev))
    bars = await s.candles("QQQ", "5m", start=t0 - timedelta(hours=1), idle_seconds=0.5)
    await task; await s.teardown()
    assert bars == [{"time": ms, "open": 1.0, "high": 2.0, "low": 0.5,
                     "close": 1.5, "volume": 100.0, "symbol": "QQQ"}]


def _fake_connect(fake, s):
    async def _c():
        s._dx = fake
        s._start_pumps()
        return True
    return _c


async def _push_later(fake, cls, ev):
    await asyncio.sleep(0.05)
    await fake._q(cls).put(ev)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_str.db && DATABASE_URL=sqlite+aiosqlite:///./_t_str.db ../.venv/bin/python -m pytest tests/test_tt_streamer.py -q; rm -f _t_str.db`
Expected: FAIL — `streamer` module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/tastytrade/streamer.py
"""One shared DXLink websocket for the whole app. Quote/Trade events are pumped into
in-memory caches (subscribe once per symbol, read many); Candle events serve history.
All failures degrade to None/[] - the facade decides about fallback. DXFeed keeps
serving last-known quotes after hours, which is the point of this migration."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle, Quote, Trade

from app.tastytrade.session import get_tt_session

logger = logging.getLogger("trading.tt_streamer")

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1d": "1d"}


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


class TTStreamer:
    def __init__(self) -> None:
        self._dx = None
        self._lock = asyncio.Lock()
        self._candle_lock = asyncio.Lock()
        self._pumps: list[asyncio.Task] = []
        self._quotes: dict[str, dict] = {}
        self._trades: dict[str, float] = {}
        self._candles: dict[str, dict[int, dict]] = {}
        self._subscribed: dict[str, set[str]] = {"Quote": set(), "Trade": set()}

    # --- connection -----------------------------------------------------
    async def _connect(self) -> bool:
        session = await get_tt_session()
        if session is None:
            return False
        try:
            self._dx = await DXLinkStreamer(session).__aenter__()
        except Exception:  # noqa: BLE001 - degrade, facade falls back
            logger.warning("DXLink connect failed", exc_info=True)
            self._dx = None
            return False
        self._start_pumps()
        return True

    async def _ensure(self) -> bool:
        if self._dx is not None:
            return True
        async with self._lock:
            if self._dx is not None:
                return True
            return await self._connect()

    def _start_pumps(self) -> None:
        self._pumps = [
            asyncio.create_task(self._pump(Quote)),
            asyncio.create_task(self._pump(Trade)),
            asyncio.create_task(self._pump(Candle)),
        ]

    async def _pump(self, cls) -> None:
        try:
            async for ev in self._dx.listen(cls):
                if cls is Quote:
                    self._quotes[ev.event_symbol] = {
                        "bid": _num(ev.bid_price), "ask": _num(ev.ask_price)}
                elif cls is Trade:
                    p = _num(ev.price)
                    if p is not None:
                        self._trades[ev.event_symbol] = p
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

    async def teardown(self) -> None:
        for t in self._pumps:
            t.cancel()
        self._pumps = []
        dx, self._dx = self._dx, None
        self._subscribed = {"Quote": set(), "Trade": set()}
        if dx is not None:
            try:
                await dx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # --- reads ------------------------------------------------------------
    async def _wait_for(self, cache: dict, symbol: str, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = cache.get(symbol)
            if v is not None:
                return v
            await asyncio.sleep(0.1)
        return cache.get(symbol)

    async def quote(self, symbol: str, timeout: float = 3.0) -> dict | None:
        if not await self._ensure():
            return None
        if symbol not in self._subscribed["Quote"]:
            await self._dx.subscribe(Quote, [symbol])
            self._subscribed["Quote"].add(symbol)
        q = await self._wait_for(self._quotes, symbol, timeout)
        return q if q and (q["bid"] is not None or q["ask"] is not None) else None

    async def trade_price(self, symbol: str, timeout: float = 3.0) -> float | None:
        if not await self._ensure():
            return None
        if symbol not in self._subscribed["Trade"]:
            await self._dx.subscribe(Trade, [symbol])
            self._subscribed["Trade"].add(symbol)
        return await self._wait_for(self._trades, symbol, timeout)

    async def candles(self, symbol: str, interval: str,
                      start: datetime, end: datetime | None = None,
                      idle_seconds: float = 2.0) -> list[dict]:
        """Collect Candle history: subscribe from `start`, wait until no new events
        arrive for `idle_seconds`, unsubscribe, return bars in [start, end] oldest-first."""
        dx_interval = _INTERVALS.get(interval)
        if dx_interval is None or not await self._ensure():
            return []
        key = f"{symbol}{{={dx_interval}}}"
        async with self._candle_lock:            # one candle collection at a time
            self._candles[key] = {}
            await self._dx.subscribe_candle([symbol], dx_interval, start_time=start)
            seen = -1
            while True:
                await asyncio.sleep(idle_seconds)
                n = len(self._candles.get(key, {}))
                if n == seen:
                    break
                seen = n
            try:
                await self._dx.unsubscribe_candle([symbol], dx_interval)
            except Exception:  # noqa: BLE001
                pass
            bars = sorted(self._candles.pop(key, {}).values(), key=lambda b: b["time"])
        lo = int(start.timestamp() * 1000)
        hi = int((end or datetime.now(timezone.utc)).timestamp() * 1000)
        return [{**b, "symbol": symbol} for b in bars
                if lo <= b["time"] <= hi and b["close"] is not None]


_streamer: TTStreamer | None = None


def get_streamer() -> TTStreamer:
    global _streamer
    if _streamer is None:
        _streamer = TTStreamer()
    return _streamer
```

- [ ] **Step 4: Run to verify it passes**

Same command as Step 2. Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tastytrade/streamer.py backend/tests/test_tt_streamer.py
git commit -m "feat(tt): shared DXLink streamer - quote/trade caches + candle history"
```

---

### Task 3: Mid sanity guard

**Files:**
- Create: `backend/app/marketdata/__init__.py` (empty), `backend/app/marketdata/sanity.py`
- Test: `backend/tests/test_mid_sanity.py`

**Interfaces:**
- Produces: `MidSanity` with `check(key: str, bid: float | None, ask: float | None, now: float | None = None) -> tuple[float | None, str | None]` — `(mid, None)` on accept, `(None, reason)` on reject. Reasons: `"missing"`, `"crossed"`, `"wide_spread"`, `"jump"`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_mid_sanity.py
"""Each sanity rule accepts/rejects correctly; the jump memory stores ACCEPTED mids only,
so one bad quote can't poison the baseline. Uses explicit `now` for determinism."""
from app.marketdata.sanity import MidSanity


def _s():
    return MidSanity(rel_spread=0.35, abs_spread=2.50, max_jump_pct=0.50, window_seconds=900)


def test_missing_or_crossed_rejected():
    s = _s()
    assert s.check("k", None, 5.0) == (None, "missing")
    assert s.check("k", 5.0, None) == (None, "missing")
    assert s.check("k", 0.0, 5.0) == (None, "missing")
    assert s.check("k", 5.2, 5.0) == (None, "crossed")


def test_wide_spread_rejected_narrow_accepted():
    s = _s()
    # mid 4.0, spread 1.0 <= max(2.50, 1.40) -> accepted
    assert s.check("k", 3.5, 4.5, now=0.0) == (4.0, None)
    # mid 4.0, spread 3.0 > max(2.50, 0.35*4.0=1.40) -> rejected
    assert s.check("k2", 2.5, 5.5, now=0.0) == (None, "wide_spread")
    # big mid: spread 3.0 <= 0.35*20=7.0 -> accepted
    assert s.check("k3", 18.5, 21.5, now=0.0) == (20.0, None)


def test_jump_rejected_within_window_and_memory_not_poisoned():
    s = _s()
    assert s.check("k", 3.9, 4.1, now=0.0) == (4.0, None)      # baseline 4.0
    mid, reason = s.check("k", 57.0, 59.0, now=60.0)           # 58 vs 4 -> jump
    assert (mid, reason) == (None, "jump")
    # baseline is still 4.0 (rejected mid NOT stored): 4.2 accepted
    assert s.check("k", 4.1, 4.3, now=120.0) == (4.2, None)
    # outside the window the jump rule doesn't apply
    assert s.check("k", 7.9, 8.1, now=120.0 + 901.0) == (8.0, None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_mid_sanity.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/marketdata/sanity.py
"""Option-mid sanity guard: a suspect quote becomes None (skip this tick), never a number.
Exists because a garbage IBKR mid once produced a $58 entry on a ~$4 calendar spread."""
from __future__ import annotations

import time


class MidSanity:
    def __init__(self, rel_spread: float | None = None, abs_spread: float | None = None,
                 max_jump_pct: float | None = None, window_seconds: int | None = None) -> None:
        from app.config import get_settings
        s = get_settings()
        self.rel_spread = rel_spread if rel_spread is not None else s.sanity_rel_spread
        self.abs_spread = abs_spread if abs_spread is not None else s.sanity_abs_spread
        self.max_jump_pct = max_jump_pct if max_jump_pct is not None else s.sanity_max_jump_pct
        self.window_seconds = (window_seconds if window_seconds is not None
                               else s.sanity_jump_window_seconds)
        self._last: dict[str, tuple[float, float]] = {}   # key -> (accepted mid, monotime)

    def check(self, key: str, bid: float | None, ask: float | None,
              now: float | None = None) -> tuple[float | None, str | None]:
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None, "missing"
        if ask < bid:
            return None, "crossed"
        mid = (bid + ask) / 2
        if (ask - bid) > max(self.abs_spread, self.rel_spread * mid):
            return None, "wide_spread"
        t = now if now is not None else time.monotonic()
        last = self._last.get(key)
        if last is not None:
            last_mid, last_t = last
            if (t - last_t) <= self.window_seconds and last_mid > 0 \
                    and abs(mid - last_mid) / last_mid > self.max_jump_pct:
                return None, "jump"           # memory keeps the ACCEPTED mid
        self._last[key] = (mid, t)
        return mid, None
```

`backend/app/marketdata/__init__.py`: empty file.

- [ ] **Step 4: Run to verify it passes**

Same command. Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/marketdata backend/tests/test_mid_sanity.py
git commit -m "feat(marketdata): mid sanity guard (crossed/wide/jump -> None)"
```

---

### Task 4: Extract IBKR data helpers (no behavior change)

**Files:**
- Create: `backend/app/ibkr/options_data.py`
- Modify: `backend/app/strategies/triple_calendar/live_source.py`
- Test: existing regressions only (this is a pure extraction)

**Interfaces:**
- Produces: `app.ibkr.options_data` with
  `async ib_option_quote(underlying: str, expiry, strike: float, right: str) -> dict | None` (normalized `{"bid","ask","last","close","price"}` or None),
  `async ib_vix(symbol: str = "VIX") -> float | None`,
  `async ib_chain_strikes(underlying: str) -> list[float]`,
  `async ib_stock_quotes(symbols: list[str], timeout: float = 3.0) -> dict[str, dict]` (re-export of `app.ibkr.market.get_quotes`).
- LivePriceSource is NOT rewired to the facade yet (Task 6) — it calls these helpers so behavior is byte-identical.

- [ ] **Step 1: Create the helper module**

```python
# backend/app/ibkr/options_data.py
"""IBKR-side market-data helpers, extracted from the TC LivePriceSource so the
marketdata facade can use IBKR as a fallback without importing strategy code.
Pure extraction - logic identical to the original live_source implementations."""
from __future__ import annotations

import asyncio

from app.ibkr.contracts import index, option, stock
from app.ibkr.gateway import get_gateway
from app.ibkr.market import get_quotes as ib_stock_quotes  # re-export  # noqa: F401
from app.ibkr.market import normalize_ticker


async def ib_option_quote(underlying: str, expiry, strike: float, right: str) -> dict | None:
    ib = await get_gateway().get_ib()
    c = option(underlying, expiry, strike, right)
    await ib.qualifyContractsAsync(c)
    # qualify silently leaves conId=0 for a strike/expiry that doesn't exist (IBKR returns
    # "Unknown contract" without raising). Degrade to "no quote" instead of a 500.
    if not getattr(c, "conId", 0):
        return None
    t = ib.reqMktData(c, "", False, False)
    await asyncio.sleep(2)
    q = normalize_ticker(t)
    ib.cancelMktData(c)
    return q


async def ib_vix(symbol: str = "VIX") -> float | None:
    """Live VIX index level (CBOE). None if unavailable - callers fail closed."""
    ib = await get_gateway().get_ib()
    c = index(symbol)
    await ib.qualifyContractsAsync(c)
    t = ib.reqMktData(c, "", False, False)
    await asyncio.sleep(2)
    q = normalize_ticker(t)
    ib.cancelMktData(c)
    return q["price"]


async def ib_chain_strikes(underlying: str) -> list[float]:
    # Strikes are returned across all expirations by reqSecDefOptParams; no per-expiry arg.
    ib = await get_gateway().get_ib()
    u = stock(underlying)
    await ib.qualifyContractsAsync(u)
    chains = await ib.reqSecDefOptParamsAsync(u.symbol, "", u.secType, u.conId)
    for ch in chains:
        if ch.exchange == "SMART":
            return sorted(float(s) for s in ch.strikes)
    return sorted(float(s) for s in chains[0].strikes) if chains else []
```

- [ ] **Step 2: Make live_source delegate**

Replace the entire body of `backend/app/strategies/triple_calendar/live_source.py` with:

```python
"""Price accessors for live Triple Calendar trading. The Phase-1 core needs SYNC reads;
run_live builds a per-tick snapshot (see _Snapshot in strategy_class) from these async
accessors, so the core stays unchanged and never does I/O. (Rewired to the marketdata
facade in the Tastytrade migration; here still IBKR helpers, extraction step.)"""
from __future__ import annotations

from app.ibkr.options_data import ib_chain_strikes, ib_option_quote, ib_stock_quotes, ib_vix


class LivePriceSource:
    async def aunderlying(self, symbol: str) -> float | None:
        return (await ib_stock_quotes([symbol]))[symbol]["price"]

    async def avix(self, symbol: str = "VIX") -> float | None:
        """Live VIX index level. None if unavailable - the caller fails closed."""
        return await ib_vix(symbol)

    async def aoption_mid(self, underlying: str, expiry, strike: float, right: str) -> float | None:
        q = await ib_option_quote(underlying, expiry, strike, right)
        return q["price"] if q else None

    async def achain(self, underlying: str) -> list[float]:
        return await ib_chain_strikes(underlying)
```

- [ ] **Step 3: Verify no behavior change**

Run:
```bash
cd backend && ../.venv/bin/python -c "import app.main; print('main imports OK')"
for t in test_tc_multi_live test_tc_vix_live test_tc_contracts_live test_tc_roll_live test_tc_live_persist; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -cE "ALL .* PASSED" | sed 's/^1$/PASS/;s/^0$/FAIL/'; rm -f _t_$t.db
done
```
Expected: `main imports OK` and 5× PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/app/ibkr/options_data.py backend/app/strategies/triple_calendar/live_source.py
git commit -m "refactor(ibkr): extract option/vix/chain data helpers from live_source"
```

---

### Task 5: The market-data facade

**Files:**
- Create: `backend/app/marketdata/facade.py`
- Test: `backend/tests/test_facade.py`

**Interfaces:**
- Consumes: Tasks 1–4 (`get_tt_session`, `get_streamer`, `MidSanity`, `app.ibkr.options_data.*`).
- Produces (all async, all in `app.marketdata.facade`):
  `stock_quote(symbol: str) -> dict | None` (`{"bid","ask","last","close","price"}`),
  `stock_quotes(symbols: list[str], timeout: float = 3.0) -> dict[str, dict]`,
  `chain_strikes(underlying: str) -> list[float]`,
  `chain_expiries(underlying: str) -> list[str]` (ISO dates),
  `option_mid(underlying: str, expiry, strike: float, right: str) -> float | None`,
  `vix() -> float | None`,
  `bars(symbol: str, timeframe: str, start, end=None) -> list[dict]`.
  Also `_race(op, tt_call, ib_call)` and module state `_chains`, `_last_source` (tests reset them).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_facade.py
"""Facade ordering: TT ok -> TT serves; TT fails + Gateway READY -> IBKR serves;
TT fails + not READY -> None; data_primary='ibkr' reverses. Sanity guard wired into
option_mid for both sources. All sources faked - no network."""
import pytest

from app.config import get_settings
from app.marketdata import facade


class _FakeStreamer:
    def __init__(self, quote=None, trade=None):
        self._q, self._t = quote, trade
    async def quote(self, symbol, timeout=3.0):
        return self._q
    async def trade_price(self, symbol, timeout=3.0):
        return self._t


def _reset():
    facade._last_source.clear()
    facade._chains.clear()
    facade._sanity._last.clear()


@pytest.mark.asyncio
async def test_tt_serves_when_available(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    monkeypatch.setattr(facade, "_get_streamer",
                        lambda: _FakeStreamer(quote={"bid": 500.0, "ask": 500.2}, trade=500.1))
    q = await facade.stock_quote("QQQ")
    assert q["price"] == 500.1 and q["bid"] == 500.0


@pytest.mark.asyncio
async def test_fallback_to_ibkr_when_ready(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_get_streamer", lambda: _FakeStreamer())  # TT empty
    monkeypatch.setattr(facade, "_ib_ready", lambda: True)
    async def fake_ib(symbols, timeout=3.0):
        return {"QQQ": {"bid": 1.0, "ask": 1.2, "last": 1.1, "close": None, "price": 1.1}}
    monkeypatch.setattr(facade, "_ib_stock_quotes", fake_ib)
    q = await facade.stock_quote("QQQ")
    assert q["price"] == 1.1


@pytest.mark.asyncio
async def test_both_down_returns_none(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_get_streamer", lambda: _FakeStreamer())
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    assert await facade.stock_quote("QQQ") is None


@pytest.mark.asyncio
async def test_option_mid_sanity_rejects_jump(monkeypatch):
    _reset()
    monkeypatch.setattr(facade, "_ib_ready", lambda: False)
    monkeypatch.setattr(facade, "_tt_chain", _fake_chain)
    good = _FakeStreamer(quote={"bid": 3.9, "ask": 4.1})
    monkeypatch.setattr(facade, "_get_streamer", lambda: good)
    from datetime import date
    e = date(2026, 7, 30)
    assert await facade.option_mid("QQQ", e, 500.0, "C") == pytest.approx(4.0)
    bad = _FakeStreamer(quote={"bid": 57.0, "ask": 59.0})   # jump vs accepted 4.0
    monkeypatch.setattr(facade, "_get_streamer", lambda: bad)
    assert await facade.option_mid("QQQ", e, 500.0, "C") is None


async def _fake_chain(underlying):
    return {"expiries": ["2026-07-30"], "strikes": [495.0, 500.0, 505.0],
            "by_key": {("2026-07-30", 500.0, "C"): ".QQQ260730C500"}}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_fac.db && DATABASE_URL=sqlite+aiosqlite:///./_t_fac.db ../.venv/bin/python -m pytest tests/test_facade.py -q; rm -f _t_fac.db`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/marketdata/facade.py
"""Provider-agnostic market data: Tastytrade primary, IBKR fallback (only while the
Gateway is READY), controlled by settings.data_primary. Consumers call these functions
and never import a provider directly. Every option mid passes the sanity guard - a
suspect quote comes back as None, never as a number. Total failure -> None/[], never
an exception into a strategy loop."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.marketdata.sanity import MidSanity

_TT_TIMEOUT = 10.0
_CHAIN_TTL = 3600.0

_sanity = MidSanity()
_chains: dict[str, tuple[float, dict]] = {}
_last_source: dict[str, str] = {}


# Indirection points so tests (and the fallback) can be faked without network.
def _get_streamer():
    from app.tastytrade.streamer import get_streamer
    return get_streamer()


def _ib_ready() -> bool:
    from app.ibkr.gateway import ConnState, get_gateway
    return get_gateway().state is ConnState.READY


async def _ib_stock_quotes(symbols, timeout: float = 3.0):
    from app.ibkr.options_data import ib_stock_quotes
    return await ib_stock_quotes(symbols, timeout=timeout)


async def _note_source(op: str, source: str) -> None:
    prev = _last_source.get(op)
    if prev is not None and prev != source:
        from app.services.journal import log_event
        await log_event(f"market data: {op} now served by {source} (was {prev})",
                        category="action")
    _last_source[op] = source


async def _race(op: str, tt_call, ib_call):
    """Try primary then fallback; a result of None/[]/{} is a miss. IBKR is only
    attempted while the Gateway is READY. Exceptions count as misses."""
    order = [("tastytrade", tt_call), ("ibkr", ib_call)]
    if get_settings().data_primary == "ibkr":
        order.reverse()
    for name, call in order:
        if call is None or (name == "ibkr" and not _ib_ready()):
            continue
        try:
            out = await asyncio.wait_for(call(), _TT_TIMEOUT)
        except Exception:  # noqa: BLE001 - a failed source is a miss, not a crash
            out = None
        if out not in (None, [], {}):
            await _note_source(op, name)
            return out
    return None


# --- stock quotes ---------------------------------------------------------
async def stock_quote(symbol: str) -> dict | None:
    async def tt():
        st = _get_streamer()
        q, last = await asyncio.gather(st.quote(symbol), st.trade_price(symbol))
        if q is None and last is None:
            return None
        bid = q["bid"] if q else None
        ask = q["ask"] if q else None
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        price = last if last is not None else mid
        return {"bid": bid, "ask": ask, "last": last, "close": None, "price": price}

    async def ib():
        return (await _ib_stock_quotes([symbol])).get(symbol)

    out = await _race("stock_quote", tt, ib)
    return out if out and out.get("price") is not None else out


async def stock_quotes(symbols: list[str], timeout: float = 3.0) -> dict[str, dict]:
    results = await asyncio.gather(*[stock_quote(s) for s in symbols])
    empty = {"bid": None, "ask": None, "last": None, "close": None, "price": None}
    return {s: (r or dict(empty)) for s, r in zip(symbols, results)}


# --- option chain ---------------------------------------------------------
async def _tt_chain(underlying: str) -> dict | None:
    """Full TT chain, cached 1h: expiries, strikes and (expiry, strike, right) ->
    streamer-symbol map used by option_mid."""
    now = time.monotonic()
    hit = _chains.get(underlying)
    if hit and now - hit[0] < _CHAIN_TTL:
        return hit[1]
    from app.tastytrade.session import get_tt_session
    s = await get_tt_session()
    if s is None:
        return None
    from tastytrade.instruments import get_option_chain
    chain = await get_option_chain(s, underlying)
    if not chain:
        return None
    by_key: dict = {}
    strikes: set[float] = set()
    for exp, options in chain.items():
        e = exp.isoformat()
        for o in options:
            r = "C" if str(getattr(o.option_type, "value", o.option_type)).upper().startswith("C") else "P"
            k = float(o.strike_price)
            strikes.add(k)
            if o.streamer_symbol:
                by_key[(e, k, r)] = o.streamer_symbol
    data = {"expiries": sorted(e.isoformat() for e in chain),
            "strikes": sorted(strikes), "by_key": by_key}
    _chains[underlying] = (now, data)
    return data


async def chain_strikes(underlying: str) -> list[float]:
    async def tt():
        d = await _tt_chain(underlying)
        return d["strikes"] if d else None

    async def ib():
        from app.ibkr.options_data import ib_chain_strikes
        return await ib_chain_strikes(underlying)

    return await _race("chain", tt, ib) or []


async def chain_expiries(underlying: str) -> list[str]:
    async def tt():
        d = await _tt_chain(underlying)
        return d["expiries"] if d else None

    async def ib():
        from app.options.chain import get_option_chain as ib_chain
        out = await ib_chain(underlying)
        return out["expirations"] or None

    return await _race("chain_expiries", tt, ib) or []


# --- option mid (sanity-guarded for BOTH sources) ---------------------------
async def option_mid(underlying: str, expiry, strike: float, right: str) -> float | None:
    e = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)
    key = f"{underlying}|{e}|{strike:g}|{right.upper()}"

    async def tt():
        d = await _tt_chain(underlying)
        sym = d["by_key"].get((e, float(strike), right.upper())) if d else None
        if not sym:
            return None
        q = await _get_streamer().quote(sym)
        if q is None:
            return None
        mid, reason = _sanity.check(key, q["bid"], q["ask"])
        if reason not in (None, "missing"):
            await _warn_sanity(key, q["bid"], q["ask"], reason)
        return mid

    async def ib():
        from app.ibkr.options_data import ib_option_quote
        q = await ib_option_quote(underlying, expiry, strike, right)
        if q is None:
            return None
        if q["bid"] is not None and q["ask"] is not None:
            mid, reason = _sanity.check(key, q["bid"], q["ask"])
        elif q["price"] is not None:
            # last/close only: no spread to validate, but the jump rule still protects.
            mid, reason = _sanity.check(key, q["price"], q["price"])
        else:
            return None
        if reason not in (None, "missing"):
            await _warn_sanity(key, q["bid"], q["ask"], reason)
        return mid

    return await _race("option_mid", tt, ib)


async def _warn_sanity(key: str, bid, ask, reason: str) -> None:
    from app.services.journal import log_event
    await log_event(f"sanity: rejected mid for {key} ({reason}; bid={bid} ask={ask})",
                    level="warning", category="decision")


# --- VIX --------------------------------------------------------------------
async def vix() -> float | None:
    async def tt():
        st = _get_streamer()
        q = await st.quote("VIX")
        if q and q["bid"] is not None and q["ask"] is not None:
            return (q["bid"] + q["ask"]) / 2
        return await st.trade_price("VIX")

    async def ib():
        from app.ibkr.options_data import ib_vix
        return await ib_vix()

    return await _race("vix", tt, ib)


# --- historical bars --------------------------------------------------------
async def bars(symbol: str, timeframe: str, start, end=None) -> list[dict]:
    if isinstance(start, datetime) and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    async def tt():
        return await _get_streamer().candles(symbol, timeframe, start=start, end=end) or None

    async def ib():
        from app.providers.ibkr_historical import IBKRHistoricalProvider
        return await IBKRHistoricalProvider().get_bars(symbol, timeframe, start=start, end=end)

    return await _race("bars", tt, ib) or []
```

Note: `bars()` uses a `_TT_TIMEOUT` of 10s via `_race`; candle collection of long ranges can exceed it — `_race` timeout applies per source and a timeout is a miss, which is acceptable for the fallback design (the historical provider in Task 8 calls the streamer directly with no cap for bulk backfills).

- [ ] **Step 4: Run to verify it passes**

Same command as Step 2. Expected: `4 passed`. Also `../.venv/bin/python -c "import app.marketdata.facade; print('ok')"`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/marketdata/facade.py backend/tests/test_facade.py
git commit -m "feat(marketdata): facade - TT primary, IBKR-when-READY fallback, sanity-guarded mids"
```

---

### Task 6: Rewire LivePriceSource + TC monitor-only mode

**Files:**
- Modify: `backend/app/strategies/triple_calendar/live_source.py`
- Modify: `backend/app/strategies/triple_calendar/strategy_class.py` (`run_live` only)
- Test: `backend/tests/test_tc_monitor_only.py` + existing regressions

**Interfaces:**
- Consumes: facade (Task 5).
- Produces: `run_live` behavior — market open + Gateway READY → full tick (unchanged); market open + NOT READY → valuation-only tick (facade data, `update_value` persisted, **no `manage_step`**, transition journaled once). Market closed → sleep (unchanged).

- [ ] **Step 1: Rewire LivePriceSource to the facade**

Replace the entire body of `backend/app/strategies/triple_calendar/live_source.py` with:

```python
"""Price accessors for live Triple Calendar trading, served by the marketdata facade
(Tastytrade primary, IBKR fallback). The Phase-1 core needs SYNC reads; run_live builds
a per-tick snapshot (see _Snapshot in strategy_class) from these async accessors."""
from __future__ import annotations

from app.marketdata import facade


class LivePriceSource:
    async def aunderlying(self, symbol: str) -> float | None:
        q = await facade.stock_quote(symbol)
        return q["price"] if q else None

    async def avix(self, symbol: str = "VIX") -> float | None:
        """VIX level via the facade. None if unavailable - the caller fails closed."""
        return await facade.vix()

    async def aoption_mid(self, underlying: str, expiry, strike: float, right: str) -> float | None:
        return await facade.option_mid(underlying, expiry, strike, right)

    async def achain(self, underlying: str) -> list[float]:
        return await facade.chain_strikes(underlying)
```

- [ ] **Step 2: Write the failing monitor-only test**

```python
# backend/tests/test_tc_monitor_only.py
"""Gateway NOT READY + market open -> the loop still values the open position via the
(faked) facade and persists update_value, but manage_step/executor are never invoked.
Run: DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db python -m tests.test_tc_monitor_only"""
import asyncio
from datetime import date, timedelta

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.strategies.triple_calendar import strategy_class as sc
from app.strategies.triple_calendar.legs import Calendar, Position
from app.strategies.triple_calendar.store import load_open_id, save_position

KEY = "strategy_2"


class _Src:
    async def aunderlying(self, s): return 500.0
    async def avix(self, s="VIX"): return 14.0
    async def aoption_mid(self, u, e, k, r): return 2.10
    async def achain(self, u): return [495.0, 500.0, 505.0]


async def _main():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, KEY) or StrategyState(key=KEY, name="TC", ready=True)
        s.add(st); st.tc_tickers = []
        from sqlalchemy import update
        await s.execute(update(OptionPositionRecord)
                        .where(OptionPositionRecord.strategy_key == KEY,
                               OptionPositionRecord.status == "open")
                        .values(status="closed"))
    today = date.today()
    pos = Position("QQQ", today, [Calendar(500.0, today + timedelta(days=21),
                                           today + timedelta(days=28))], net_debit=4.0)
    rid = await save_position(KEY, pos, order_ids=[], paper=True)

    strat = sc.TripleCalendarStrategy()
    calls = {"manage": 0}

    async def boom(*a, **kw):
        calls["manage"] += 1
        raise AssertionError("manage_step must not run while NOT READY")

    sc_ns = vars(sc)
    orig = dict(manage_step=sc_ns["manage_step"], LivePriceSource=sc_ns["LivePriceSource"])
    sc_ns["manage_step"] = boom
    sc_ns["LivePriceSource"] = _Src

    class _GW:  # NOT READY
        state = type("S", (), {"name": "CONNECTED"})()
    sc_ns_gw = sc_ns["get_gateway"]
    sc_ns["get_gateway"] = lambda: _GW()
    sc_ns["get_market_status"] = lambda: {"is_open": True}

    task = asyncio.create_task(strat.run_live())
    await asyncio.sleep(1.0)          # let at least one valuation tick run
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    sc_ns.update(orig); sc_ns["get_gateway"] = sc_ns_gw

    assert calls["manage"] == 0
    async with session_scope() as s:
        rec = await s.get(OptionPositionRecord, rid)
        assert rec.last_value is not None, "valuation-only tick must persist update_value"
    assert await load_open_id(KEY, "QQQ") == rid   # still open, untouched
    print("ALL TC MONITOR-ONLY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && rm -f _t_mon.db && DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db ../.venv/bin/python -m tests.test_tc_monitor_only; rm -f _t_mon.db`
Expected: FAIL — with the current code the NOT-READY branch just sleeps, so `rec.last_value is None` fails the assert.

- [ ] **Step 4: Implement monitor-only ticks**

In `backend/app/strategies/triple_calendar/strategy_class.py`, `run_live`, replace the block:

```python
        while True:
            # Pause unless READY AND the regular session is open — no trading on stale quotes.
            if get_gateway().state is not ConnState.READY or not get_market_status()["is_open"]:
                await asyncio.sleep(get_settings().tc_manage_interval_seconds)
                continue
```

with:

```python
        monitor_logged = False
        while True:
            # Market closed: nothing to do at all.
            if not get_market_status()["is_open"]:
                await asyncio.sleep(get_settings().tc_manage_interval_seconds)
                continue
            # Gateway state decides the tick kind: READY -> full (may trade);
            # NOT READY -> monitor-only (facade data still flows; valuations persist;
            # no order actions). Journal the transition once per state change.
            ready = get_gateway().state is ConnState.READY
            if not ready and not monitor_logged:
                await log_event("Triple Calendar: monitor-only (Gateway not READY - "
                                "valuations continue, order actions paused)",
                                category="decision", strategy_key=self.key)
                monitor_logged = True
            elif ready and monitor_logged:
                await log_event("Triple Calendar: Gateway READY - trading resumed",
                                category="decision", strategy_key=self.key)
                monitor_logged = False
```

Then, inside the per-ticker `try:` block, wrap the `manage_step` + `_persist` calls so they only run when `ready`, leaving the valuation persist to run in both modes. The block currently reads:

```python
                        strikes = await src.achain(u)
                        snapshot = await _resolve_snapshot(src, params, spot, strikes, today, pos)
                        out = await manage_step(today, snapshot, params, executor, pos,
                                                vix=vix, contracts=contracts)
                        pos, rid = await self._persist(out, rid)
                        positions[u], record_ids[u] = pos, rid
```

Change it to:

```python
                        strikes = await src.achain(u)
                        snapshot = await _resolve_snapshot(src, params, spot, strikes, today, pos)
                        if ready:
                            out = await manage_step(today, snapshot, params, executor, pos,
                                                    vix=vix, contracts=contracts)
                            pos, rid = await self._persist(out, rid)
                            positions[u], record_ids[u] = pos, rid
```

(The `# Persist the live valuation ...` block below stays outside the `if ready:` so valuation-only ticks persist `update_value`; it already guards on `pos is not None`.)

Also change the flat-ticker fast-poll condition to only apply when trading is possible — replace:

```python
            any_flat_blocked = (vix is None or vix > vmax) and any(
                positions.get(tk["ticker"]) is None for tk in tickers)
```

with:

```python
            any_flat_blocked = ready and (vix is None or vix > vmax) and any(
                positions.get(tk["ticker"]) is None for tk in tickers)
```

- [ ] **Step 5: Run the new test + regressions**

```bash
cd backend && rm -f _t_mon.db && DATABASE_URL=sqlite+aiosqlite:///./_t_mon.db ../.venv/bin/python -m tests.test_tc_monitor_only; rm -f _t_mon.db
for t in test_tc_multi_live test_tc_vix_live test_tc_contracts_live test_tc_roll_live test_tc_live_persist; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -cE "ALL .* PASSED" | sed 's/^1$/PASS/;s/^0$/FAIL/'; rm -f _t_$t.db
done
../.venv/bin/python -c "import app.main; print('main imports OK')"
```
Expected: monitor-only prints `ALL TC MONITOR-ONLY TESTS PASSED`; 5× PASS; imports OK. (The existing live tests fake `LivePriceSource`/gateway themselves, so the facade rewire is transparent to them; if one fails on an import path, fix the test's monkeypatch target, not the code.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/strategies/triple_calendar/live_source.py backend/app/strategies/triple_calendar/strategy_class.py backend/tests/test_tc_monitor_only.py
git commit -m "feat(tc): facade-served prices + monitor-only mode when Gateway not READY"
```

---

### Task 7: Rewire the remaining quote consumers

**Files:**
- Modify: `backend/app/services/market.py` (`get_market_overview`)
- Modify: `backend/app/routers/strategies.py` (position live-quote block, ~line 168–180)
- Modify: `backend/app/options/chain.py` (`get_chain_quotes` spot; Builder expiries stay IBKR-shaped via facade)
- Test: `backend/tests/test_consumers_facade.py`

**Interfaces:**
- Consumes: `facade.stock_quote`, `facade.stock_quotes`, `facade.chain_expiries`, `facade.chain_strikes`.

- [ ] **Step 1: Overview via facade (drop the IBKR-auth early-return)**

In `backend/app/services/market.py`, `get_market_overview`:
- Delete the block:
```python
    if not session_manager.is_authenticated():
        base["note"] = "Live market data requires an IBKR Gateway connection."
        base["indices"] = [{**ix, **_NULL_QUOTE} for ix in INDICES]
        return base
```
- Replace `get_quotes(symbols, timeout=2.5),` in the `asyncio.gather` with:
```python
        facade_stock_quotes(symbols, timeout=2.5),
```
- At the top of the file, replace `from app.ibkr.market import get_quotes` with:
```python
from app.marketdata.facade import stock_quotes as facade_stock_quotes
```
- After the gather's exception handling, add a no-data note when every price is None:
```python
    if quotes and all(q.get("price") is None for q in quotes.values()):
        base["note"] = "No market data source available (Tastytrade not configured, Gateway down)."
```

- [ ] **Step 2: Position live quote via facade**

In `backend/app/routers/strategies.py` (~line 168), replace:

```python
    live = None
    if session_manager.is_authenticated():
        try:
            from app.ibkr.market import get_quotes
            q = (await get_quotes([symbol], timeout=3.0)).get(symbol, {})
            live = q.get("last")
            if live is None and q.get("bid") is not None and q.get("ask") is not None:
                live = round((q["bid"] + q["ask"]) / 2, 4)
        except Exception:  # noqa: BLE001 — never fail the page on a quote hiccup
            logger.exception("position quote fetch failed for %s", symbol)
```

with:

```python
    live = None
    try:
        from app.marketdata.facade import stock_quote
        q = (await stock_quote(symbol)) or {}
        live = q.get("last")
        if live is None and q.get("bid") is not None and q.get("ask") is not None:
            live = round((q["bid"] + q["ask"]) / 2, 4)
        if live is None:
            live = q.get("price")
    except Exception:  # noqa: BLE001 — never fail the page on a quote hiccup
        logger.exception("position quote fetch failed for %s", symbol)
```

- [ ] **Step 3: Builder chain via facade**

In `backend/app/options/chain.py`:
- Replace `from app.ibkr.market import get_quotes` with `from app.marketdata import facade`.
- In `get_option_chain`, keep the IBKR implementation but rename it `_ib_option_chain` (drop the `async def get_option_chain` name), and add a facade-first wrapper with the same public name and return shape:

```python
async def get_option_chain(underlying: str) -> dict:
    expirations = await facade.chain_expiries(underlying)
    strikes = await facade.chain_strikes(underlying)
    if expirations and strikes:
        return {"underlying": underlying.upper(),
                "expirations": expirations, "strikes": strikes}
    return await _ib_option_chain(underlying)
```

  (Note: `facade.chain_expiries`'s own IBKR fallback imports `app.options.chain.get_option_chain`; point that import at `_ib_option_chain` instead to avoid recursion — update the facade's `chain_expiries` `ib()` inner to `from app.options.chain import _ib_option_chain as ib_chain`.)
- In `get_chain_quotes`, replace:
```python
    q = await get_quotes([underlying])
    spot = q.get(underlying, {}).get("price")
```
with:
```python
    q = await facade.stock_quote(underlying)
    spot = (q or {}).get("price")
```
(`get_leg_mids` needs no change — `LivePriceSource` already delegates to the facade.)

- [ ] **Step 4: Write the test**

```python
# backend/tests/test_consumers_facade.py
"""Consumers read through the facade: overview builds rows without IBKR auth; the
builder chain composes facade expiries+strikes and falls back to IBKR shape."""
import pytest

from app.marketdata import facade


@pytest.mark.asyncio
async def test_overview_without_gateway(monkeypatch):
    from app.services import market as m
    async def fake_quotes(symbols, timeout=2.5):
        return {s: {"bid": 1.0, "ask": 1.2, "last": 1.1, "close": None, "price": 1.1}
                for s in symbols}
    monkeypatch.setattr(m, "facade_stock_quotes", fake_quotes)
    async def fake_weekly(symbols):
        return {}
    monkeypatch.setattr(m, "_weekly_changes", fake_weekly)
    out = await m.get_market_overview()
    assert out["indices"] and out["indices"][0]["price"] == 1.1


@pytest.mark.asyncio
async def test_builder_chain_prefers_facade(monkeypatch):
    from app.options import chain as ch
    async def fake_exp(u): return ["2026-07-30"]
    async def fake_strikes(u): return [495.0, 500.0]
    monkeypatch.setattr(ch.facade, "chain_expiries", fake_exp)
    monkeypatch.setattr(ch.facade, "chain_strikes", fake_strikes)
    out = await ch.get_option_chain("QQQ")
    assert out == {"underlying": "QQQ", "expirations": ["2026-07-30"],
                   "strikes": [495.0, 500.0]}
```

- [ ] **Step 5: Run**

Run: `cd backend && rm -f _t_con.db && DATABASE_URL=sqlite+aiosqlite:///./_t_con.db ../.venv/bin/python -m pytest tests/test_consumers_facade.py tests/test_facade.py -q; rm -f _t_con.db && ../.venv/bin/python -c "import app.main; print('main imports OK')"`
Expected: all pass, imports OK.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/market.py backend/app/routers/strategies.py backend/app/options/chain.py backend/app/marketdata/facade.py backend/tests/test_consumers_facade.py
git commit -m "feat(marketdata): overview, position quote, and builder chain read through the facade"
```

---

### Task 8: Tastytrade historical provider

**Files:**
- Create: `backend/app/providers/tastytrade_historical.py`
- Modify: `backend/app/providers/__init__.py`
- Test: `backend/tests/test_tt_historical.py`

**Interfaces:**
- Consumes: `TTStreamer.candles` (Task 2), `tt_configured` (Task 1).
- Produces: `TastytradeHistoricalProvider(HistoricalDataProvider)` with `name = "tastytrade"`; factory `get_historical_provider()` returns it when TT creds are configured, else `IBKRHistoricalProvider` (unchanged behavior without creds).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tt_historical.py
"""TastytradeHistoricalProvider normalizes streamer candles into provider Bars, and the
factory prefers it only when creds are configured."""
from datetime import datetime, timezone

import pytest

import app.providers as providers
from app.providers.tastytrade_historical import TastytradeHistoricalProvider


@pytest.mark.asyncio
async def test_get_bars_delegates_to_streamer(monkeypatch):
    p = TastytradeHistoricalProvider()
    async def fake_candles(symbol, interval, start, end=None, idle_seconds=2.0):
        return [{"time": 1000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                 "volume": 10.0, "symbol": symbol}]
    monkeypatch.setattr(p, "_candles", fake_candles)
    bars = await p.get_bars("QQQ", "5m", start=datetime(2026, 7, 8, tzinfo=timezone.utc))
    assert bars[0]["close"] == 1.5 and bars[0]["symbol"] == "QQQ"


def test_factory_prefers_tt_when_configured(monkeypatch):
    providers.get_historical_provider.cache_clear()
    monkeypatch.setattr(providers, "tt_configured", lambda: True)
    assert providers.get_historical_provider().name == "tastytrade"
    providers.get_historical_provider.cache_clear()
    monkeypatch.setattr(providers, "tt_configured", lambda: False)
    assert providers.get_historical_provider().name != "tastytrade"
    providers.get_historical_provider.cache_clear()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_tt_historical.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/providers/tastytrade_historical.py
"""Historical OHLCV bars via Tastytrade DXLink Candle events (already normalized by the
streamer to the provider Bar shape). Used by the factory when TT creds are configured."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone

from app.providers.base import TIMEFRAMES, HistoricalDataProvider, ProviderError

# Default lookback when no start is given, per timeframe (mirrors IBKR provider limits).
_DEFAULT_LOOKBACK = {"1m": timedelta(days=5), "5m": timedelta(days=30),
                     "15m": timedelta(days=60), "1d": timedelta(days=365 * 2)}


class TastytradeHistoricalProvider(HistoricalDataProvider):
    name = "tastytrade"

    async def _candles(self, symbol, interval, start, end=None, idle_seconds=2.0):
        from app.tastytrade.streamer import get_streamer
        return await get_streamer().candles(symbol, interval, start=start, end=end,
                                            idle_seconds=idle_seconds)

    async def get_bars(self, symbol, timeframe, start=None, end=None):
        if timeframe not in TIMEFRAMES:
            raise ProviderError(f"unsupported timeframe {timeframe!r}")
        now = datetime.now(timezone.utc)
        if start is None:
            start = now - _DEFAULT_LOOKBACK[timeframe]
        if isinstance(start, date) and not isinstance(start, datetime):
            start = datetime.combine(start, dtime.min, tzinfo=timezone.utc)
        if isinstance(end, date) and not isinstance(end, datetime):
            end = datetime.combine(end, dtime.max, tzinfo=timezone.utc)
        return await self._candles(symbol, timeframe, start=start, end=end)
```

In `backend/app/providers/__init__.py`, replace the factory body with:

```python
"""Provider factory. Historical/forward-refresh data prefers Tastytrade (DXLink candles)
when creds are configured; IBKR remains the fallback. Bulk backtests read cached files
directly via history_cache."""

from __future__ import annotations

from functools import lru_cache

from app.providers.base import HistoricalDataProvider, ProviderError
from app.providers.ibkr_historical import IBKRHistoricalProvider
from app.tastytrade.session import tt_configured

__all__ = ["get_historical_provider", "HistoricalDataProvider", "ProviderError"]


@lru_cache
def get_historical_provider() -> HistoricalDataProvider:
    if tt_configured():
        from app.providers.tastytrade_historical import TastytradeHistoricalProvider
        return TastytradeHistoricalProvider()
    return IBKRHistoricalProvider()
```

- [ ] **Step 4: Run to verify it passes**

Same command. Expected: `2 passed`. Also `../.venv/bin/python -c "import app.main; print('main imports OK')"`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/providers/tastytrade_historical.py backend/app/providers/__init__.py backend/tests/test_tt_historical.py
git commit -m "feat(providers): Tastytrade historical bars, factory prefers TT when configured"
```

---

### Task 9: Credentials endpoint + API tab card

**Files:**
- Modify: `backend/app/auth/routes.py` (add GET/PUT `/tastytrade`)
- Modify: `frontend/src/api.js`, `frontend/src/components/ApiTab.jsx`
- Test: `backend/tests/test_tt_creds_api.py`

**Interfaces:**
- Produces: `GET /api/auth/tastytrade` → `{configured: bool, has_secret: bool, has_refresh: bool, data_primary: str}` (never echoes secrets); `PUT /api/auth/tastytrade` body `{provider_secret?, refresh_token?}` → persists to `backend/.env` (upsert `TT_PROVIDER_SECRET=` / `TT_REFRESH_TOKEN=` lines), updates the cached `Settings` instance, calls `reset_tt_session()`, returns the GET shape. `api.ttStatus()`, `api.ttSave(body)` in the frontend.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tt_creds_api.py
"""PUT /auth/tastytrade persists creds to the .env file, updates live settings, and
resets the cached TT session; GET reports configured-ness without echoing secrets."""
import pytest

from app.auth import routes as auth_routes
from app.config import get_settings


@pytest.mark.asyncio
async def test_put_persists_and_get_masks(tmp_path, monkeypatch):
    envfile = tmp_path / ".env"
    monkeypatch.setattr(auth_routes, "_ENV_PATH", envfile)
    reset_calls = []
    monkeypatch.setattr(auth_routes, "reset_tt_session", lambda: reset_calls.append(1))
    monkeypatch.setattr(get_settings(), "tt_provider_secret", "")
    monkeypatch.setattr(get_settings(), "tt_refresh_token", "")

    out = await auth_routes.tt_save(auth_routes.TTCredsBody(
        provider_secret="sek", refresh_token="ref"))
    assert out["configured"] is True and out["has_secret"] and out["has_refresh"]
    assert "sek" not in str(out) and "ref" not in str(out)
    text = envfile.read_text()
    assert "TT_PROVIDER_SECRET=sek" in text and "TT_REFRESH_TOKEN=ref" in text
    assert get_settings().tt_provider_secret == "sek"
    assert reset_calls, "session cache must be reset after creds change"

    # upsert (not append-duplicate) on second save
    await auth_routes.tt_save(auth_routes.TTCredsBody(provider_secret="sek2"))
    text = envfile.read_text()
    assert text.count("TT_PROVIDER_SECRET=") == 1 and "sek2" in text
    assert "TT_REFRESH_TOKEN=ref" in text          # untouched field kept

    status = await auth_routes.tt_status()
    assert status["configured"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_cred.db && DATABASE_URL=sqlite+aiosqlite:///./_t_cred.db ../.venv/bin/python -m pytest tests/test_tt_creds_api.py -q; rm -f _t_cred.db`
Expected: FAIL — `TTCredsBody`/`tt_save` undefined.

- [ ] **Step 3: Implement the endpoints**

In `backend/app/auth/routes.py`, add (imports at top: `from pathlib import Path`, `from pydantic import BaseModel`, `from app.config import get_settings`, `from app.tastytrade.session import reset_tt_session`; module-level constant next to them):

```python
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"   # backend/.env


class TTCredsBody(BaseModel):
    provider_secret: str | None = None
    refresh_token: str | None = None


def _upsert_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    for key, val in values.items():
        row = f"{key}={val}"
        for i, ln in enumerate(lines):
            if ln.startswith(f"{key}="):
                lines[i] = row
                break
        else:
            lines.append(row)
    path.write_text("\n".join(lines) + "\n")


def _tt_state() -> dict:
    s = get_settings()
    return {"configured": bool(s.tt_provider_secret and s.tt_refresh_token),
            "has_secret": bool(s.tt_provider_secret),
            "has_refresh": bool(s.tt_refresh_token),
            "data_primary": s.data_primary}


@router.get("/tastytrade")
async def tt_status() -> dict:
    return _tt_state()


@router.put("/tastytrade")
async def tt_save(body: TTCredsBody) -> dict:
    """Persist Tastytrade OAuth creds to backend/.env and apply them live (no restart)."""
    s = get_settings()
    updates: dict[str, str] = {}
    if body.provider_secret is not None:
        s.tt_provider_secret = body.provider_secret.strip()
        updates["TT_PROVIDER_SECRET"] = s.tt_provider_secret
    if body.refresh_token is not None:
        s.tt_refresh_token = body.refresh_token.strip()
        updates["TT_REFRESH_TOKEN"] = s.tt_refresh_token
    if updates:
        _upsert_env(_ENV_PATH, updates)
        reset_tt_session()
    return _tt_state()
```

- [ ] **Step 4: Run to verify it passes**

Same command as Step 2. Expected: `1 passed`. Also `../.venv/bin/python -c "import app.main; print('main imports OK')"`.

- [ ] **Step 5: Frontend — api.js + ApiTab card**

In `frontend/src/api.js`, next to `authStatus`/`connect`, add:

```javascript
  // Tastytrade market-data creds (OAuth personal grant).
  ttStatus: () => request("/api/auth/tastytrade"),
  ttSave: (body) =>
    request("/api/auth/tastytrade", { method: "PUT", body: JSON.stringify(body) }),
```

In `frontend/src/components/ApiTab.jsx`, add state after the existing hooks:

```javascript
  const [tt, setTt] = useState(null);
  const [ttSecret, setTtSecret] = useState("");
  const [ttRefresh, setTtRefresh] = useState("");
  const [ttMsg, setTtMsg] = useState(null);
  const loadTt = () => api.ttStatus().then(setTt).catch(() => {});
  useEffect(() => { loadTt(); }, []);
  const saveTt = async () => {
    setTtMsg(null);
    try {
      const body = {};
      if (ttSecret.trim()) body.provider_secret = ttSecret.trim();
      if (ttRefresh.trim()) body.refresh_token = ttRefresh.trim();
      const res = await api.ttSave(body);
      setTt(res); setTtSecret(""); setTtRefresh("");
      setTtMsg(res.configured ? "Saved — Tastytrade data enabled." : "Saved.");
    } catch (e) { setTtMsg(e.message || "Save failed"); }
  };
```

and after the closing `</section>` of the "Connection" card, add:

```javascript
      <section className="card">
        <div className="card-head">
          <h2>Tastytrade Market Data</h2>
          <span className={`mode-badge ${tt?.configured ? "live" : "disabled"}`}>
            {tt?.configured ? "Configured" : "Not configured"}
          </span>
        </div>
        <p className="muted small">
          Primary market-data source (quotes, option chains/mids, VIX, history). Create an
          OAuth personal grant at my.tastytrade.com → API access, then paste the client
          secret and refresh token here. Stored server-side in <code>.env</code>; IBKR is
          used as fallback while its Gateway is connected.
        </p>
        <div className="row gap center" style={{ marginTop: 10 }}>
          <input className="input" type="password" placeholder={tt?.has_secret ? "Provider secret (saved)" : "Provider secret"}
            value={ttSecret} onChange={(e) => setTtSecret(e.target.value)} />
          <input className="input" type="password" placeholder={tt?.has_refresh ? "Refresh token (saved)" : "Refresh token"}
            value={ttRefresh} onChange={(e) => setTtRefresh(e.target.value)} />
          <button className="btn primary" onClick={saveTt}>Save</button>
        </div>
        {ttMsg && <div className="muted small" style={{ marginTop: 8 }}>{ttMsg}</div>}
      </section>
```

- [ ] **Step 6: Build check + commit**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → expect `✓ built in …`.

```bash
git add backend/app/auth/routes.py backend/tests/test_tt_creds_api.py frontend/src/api.js frontend/src/components/ApiTab.jsx
git commit -m "feat(tt): creds endpoint (persist to .env, live-apply) + API tab card"
```

---

### Task 10: Full regression, deploy, live smoke

**Files:** none (verification; commit only if fixes were needed).

- [ ] **Step 1: Backend suite + standalone regressions**

```bash
cd backend && ../.venv/bin/python -c "import app.main; print('main imports OK')"
rm -f _t_all.db
DATABASE_URL=sqlite+aiosqlite:///./_t_all.db ../.venv/bin/python -m pytest \
  tests/test_tt_session.py tests/test_tt_streamer.py tests/test_mid_sanity.py \
  tests/test_facade.py tests/test_consumers_facade.py tests/test_tt_historical.py \
  tests/test_tt_creds_api.py tests/test_tc_multi_store.py tests/test_tc_tickers_api.py \
  tests/test_tc_multi_endpoint.py tests/test_tc_options_endpoint.py tests/test_contracts_api.py -q
rm -f _t_all.db
for t in test_tc_monitor_only test_tc_multi_live test_tc_vix_live test_tc_contracts_live test_tc_roll_live test_tc_live_persist; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -cE "ALL .* PASSED" | sed 's/^1$/PASS/;s/^0$/FAIL/'; rm -f _t_$t.db
done
```
Expected: all pytest pass; 6× PASS.

- [ ] **Step 2: Rebuild + restart + endpoint smoke**

```bash
cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep "built in"
cd .. && launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
for i in $(seq 1 45); do curl -s -m3 http://127.0.0.1:8765/health 2>/dev/null | grep -q '"status":"ok"' && break; sleep 1; done
curl -s -m5 http://127.0.0.1:8765/api/auth/tastytrade | python3 -m json.tool
curl -s -m15 http://127.0.0.1:8765/api/dashboard/market/overview | python3 -c "import sys,json;d=json.load(sys.stdin);print('note:',d.get('note'));print('rows:',len(d.get('indices',[])))" 2>/dev/null || echo "overview endpoint path differs - check routers/dashboard.py"
```
Expected: health ok; tastytrade status `{configured: false, ...}` until the user saves creds.

- [ ] **Step 3: Live smoke (needs the user's TT creds saved in the API tab)**

This is the payoff check and requires real credentials — coordinate with the user:
```bash
curl -s -m20 "http://127.0.0.1:8765/api/builder/chain-quotes?underlying=QQQ&expiry=<~21DTE date>&count=5" | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = d.get('rows') or []
miss = sum(1 for r in rows for f in ('call_mid','put_mid') if r.get(f) in (None, 0))
print(f'{len(rows)} strikes, {miss} missing mids')"
```
Expected with creds configured (even after hours): substantially fewer missing mids than the IBKR baseline (which was 100% missing at 21 DTE after hours on 2026-07-09). Without creds: unchanged IBKR behavior (fallback intact).

- [ ] **Step 4: Commit any fixes**

```bash
git add -A && git commit -m "test(tt): end-to-end verification fixes" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** session manager (T1); DXLink streamer incl. candles (T2); sanity guard w/ exact defaults (T3); IBKR helper extraction so the fallback doesn't import strategy code (T4); facade with try-order, READY-gated fallback, transition-only journaling, sanity on both sources (T5); LivePriceSource rewire + monitor-only mode (T6); overview/position/builder consumers + dropped `is_authenticated` gates (T7); historical provider + factory preference (T8); OAuth creds endpoint + API tab card (T9); regression + live smoke (T10). `market_stream.py` intentionally untouched (spec scope note).
- **Type consistency:** Bar shape `{"time","open","high","low","close","volume","symbol"}` identical across streamer (T2), facade `bars` (T5), provider (T8), and `providers/base.py`. `quote()` returns `{"bid","ask"}`; `stock_quote` returns the 5-key normalized dict matching `normalize_ticker`. `MidSanity.check -> (mid, reason)` used consistently in T3/T5.
- **Placeholder scan:** clean; every code step carries the code. The one deliberate deferral: dashboard Data/Broker status chips (spec Frontend section) are reduced to the ApiTab configured-badge — the dashboard chip needs a design pass and can ride a later UI change; noted here so the final review can call it out if the user wants it now.
- **Known risks for implementers:** `DXLinkStreamer.__aenter__` usage outside `async with` (verify at T2 against the installed SDK); tastytrade `Quote` field types are Decimal (handled by `_num`); `get_settings()` is `@lru_cache`d so tests mutate the cached instance via monkeypatch.setattr (shown in tests).
