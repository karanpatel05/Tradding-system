# IBKR Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Tastytrade (live/execution) + Massive (historical) backend with a single Interactive Brokers backend via IB Gateway (paper trading), reusing existing seams so callers are untouched.

**Architecture:** New `app/ibkr/` package (connection, market data, accounts) backed by `ib_async`; `engine/execution.py` rewritten against IBKR with the same public API; historical provider swapped to IBKR for forward refresh while backtests keep reading cached `data_cache/*.json`; OAuth auth replaced by Gateway connection state; Tastytrade/Massive code deleted.

**Tech Stack:** Python 3.14, FastAPI (async), `ib_async` 2.1.0, pydantic-settings, pytest.

**Spec:** `docs/superpowers/specs/2026-06-18-ibkr-migration-design.md`

> **Git note:** the repo is not currently under version control. Task 1 offers `git init`. If you decline git, treat every "Commit" step as a checkpoint (run tests, move on). Commit messages end with the Co-Authored-By trailer.

> **Testing note:** anything that talks to the Gateway cannot be exercised on the dev box (no Gateway reachable). Those pieces are unit-tested by stubbing the `IB` object; the live path is validated by the user via `scripts/connect_smoke.py` (Task 12). Run all `pytest` from the `backend/` directory.

---

### Task 1: Dependencies & project prep

**Files:**
- Modify: `backend/requirements.txt` (or `pyproject.toml` if that's the dep source)

- [ ] **Step 1: (Optional) initialize git for checkpointing**

```bash
cd /Users/karanpatel/PycharmProjects/Tradding-system
git init && printf "%s\n" ".venv/" "__pycache__/" "*.pyc" "data_cache/" "*.db" ".session_store.json" ".env" > .gitignore
git add -A && git commit -m "chore: baseline before IBKR migration"
```

- [ ] **Step 2: Install ib_async into the venv**

Run: `cd backend && ../.venv/bin/pip install ib_async==2.1.0`
Expected: installs `ib_async`, `aeventkit`, `nest_asyncio`, `tzdata`.

- [ ] **Step 3: Verify it imports on Python 3.14**

Run: `../.venv/bin/python -c "from ib_async import IB, Stock, MarketOrder, LimitOrder, StopOrder; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Record the dependency**

Add to `backend/requirements.txt`:
```
ib_async==2.1.0
```
Remove the `tastytrade` line (and any explicit `greenlet` line) from `backend/requirements.txt`.

- [ ] **Step 5: Commit**

```bash
git add backend/requirements.txt && git commit -m "build: add ib_async, drop tastytrade dependency

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Config — swap settings to IBKR

**Files:**
- Modify: `backend/app/config.py:50-59` (the tastytrade/massive blocks)
- Test: `backend/tests/test_config_ibkr.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_config_ibkr.py
from app.config import Settings


def test_ibkr_defaults_present_and_old_removed():
    s = Settings()
    assert s.ibkr_host == "127.0.0.1"
    assert s.ibkr_port == 4002          # paper Gateway
    assert s.ibkr_client_id == 1
    assert s.ibkr_account == ""         # blank -> auto-detect first managed acct
    assert s.ibkr_market_data_type == 1
    assert s.enable_live_orders is False
    # old backends gone
    assert not hasattr(s, "massive_api_key")
    assert not hasattr(s, "tastytrade_provider_secret")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_config_ibkr.py -v`
Expected: FAIL (ibkr_host missing / old attrs still present).

- [ ] **Step 3: Edit config**

In `backend/app/config.py`, delete these lines:
```python
    tastytrade_provider_secret: str | None = None
    tastytrade_is_test: bool = False
    massive_api_key: str | None = None
    massive_base_url: str = "https://api.polygon.io"
```
Add in their place:
```python
    # Interactive Brokers (IB Gateway, paper trading).
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002            # paper Gateway (TWS paper = 7497)
    ibkr_client_id: int = 1
    ibkr_account: str = ""           # blank -> first managed account
    ibkr_market_data_type: int = 1   # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
```
Then search `config.py` for any `massive_configured` / `tastytrade` helper properties and delete them.

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_config_ibkr.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_config_ibkr.py
git commit -m "feat(config): replace tastytrade/massive settings with IBKR

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: IBKR contract helper

**Files:**
- Create: `backend/app/ibkr/__init__.py` (empty)
- Create: `backend/app/ibkr/contracts.py`
- Test: `backend/tests/test_ibkr_contracts.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_ibkr_contracts.py
from app.ibkr.contracts import stock


def test_stock_contract_shape():
    c = stock("NVDA")
    assert c.symbol == "NVDA"
    assert c.secType == "STK"
    assert c.exchange == "SMART"
    assert c.currency == "USD"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_contracts.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# backend/app/ibkr/contracts.py
"""IBKR contract helpers. One place that builds the Stock contracts used across
market data, history, and execution so routing (SMART/USD) stays consistent."""
from __future__ import annotations

from ib_async import Stock


def stock(symbol: str) -> Stock:
    return Stock(symbol.upper(), "SMART", "USD")
```
Also create empty `backend/app/ibkr/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_contracts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/__init__.py backend/app/ibkr/contracts.py backend/tests/test_ibkr_contracts.py
git commit -m "feat(ibkr): add Stock contract helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Gateway connection manager

**Files:**
- Create: `backend/app/ibkr/gateway.py`
- Test: `backend/tests/test_ibkr_gateway.py`

- [ ] **Step 1: Write the failing test (connection-state logic, no real socket)**

```python
# backend/tests/test_ibkr_gateway.py
import pytest
from app.ibkr import gateway as gw


class _FakeIB:
    def __init__(self): self._conn = False
    def isConnected(self): return self._conn
    async def connectAsync(self, host, port, clientId, readonly=False): self._conn = True
    def reqMarketDataType(self, t): self.mdt = t
    def disconnect(self): self._conn = False


@pytest.mark.asyncio
async def test_connect_sets_connected_and_market_data_type(monkeypatch):
    fake = _FakeIB()
    g = gw.Gateway(ib_factory=lambda: fake)
    assert g.is_connected() is False
    await g.connect()
    assert g.is_connected() is True
    assert fake.mdt == 1            # from settings default
    g.disconnect()
    assert g.is_connected() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_gateway.py -v`
Expected: FAIL (module/attr missing).

- [ ] **Step 3: Implement**

```python
# backend/app/ibkr/gateway.py
"""Singleton IB Gateway connection manager (ib_async).

'Authenticated' in this system means the Gateway is connected and the API is
reachable. ``get_ib()`` returns a connected IB instance, connecting on first use.
``ib_factory`` is injectable so tests can stub the IB object."""
from __future__ import annotations

import logging
from typing import Callable

from app.config import get_settings

logger = logging.getLogger("trading.ibkr")


class Gateway:
    def __init__(self, ib_factory: Callable | None = None):
        self._ib_factory = ib_factory or self._default_factory
        self._ib = None

    @staticmethod
    def _default_factory():
        from ib_async import IB
        return IB()

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def connect(self):
        if self.is_connected():
            return self._ib
        s = get_settings()
        ib = self._ib or self._ib_factory()
        await ib.connectAsync(s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, readonly=False)
        ib.reqMarketDataType(s.ibkr_market_data_type)
        self._ib = ib
        logger.warning("Connected to IB Gateway %s:%s clientId=%s", s.ibkr_host, s.ibkr_port, s.ibkr_client_id)
        return ib

    async def get_ib(self):
        if not self.is_connected():
            await self.connect()
        return self._ib

    def disconnect(self):
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()


_gateway = Gateway()


def get_gateway() -> Gateway:
    return _gateway
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_gateway.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/gateway.py backend/tests/test_ibkr_gateway.py
git commit -m "feat(ibkr): add Gateway connection manager

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Accounts & positions (normalized)

**Files:**
- Create: `backend/app/ibkr/account.py`
- Test: `backend/tests/test_ibkr_account.py`

The kill switch / dashboard consume positions as objects with `.symbol`, `.quantity`,
`.quantity_direction` ("Long"/"Short"), `.instrument_type` ("Equity"). We normalize IBKR
`Position` into a small dataclass with those exact attributes.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_ibkr_account.py
from app.ibkr.account import normalize_position, NormalizedPosition


class _C:  # fake ib_async Contract
    def __init__(self, symbol, secType="STK"): self.symbol = symbol; self.secType = secType


class _P:  # fake ib_async Position
    def __init__(self, account, contract, position, avgCost):
        self.account, self.contract, self.position, self.avgCost = account, contract, position, avgCost


def test_normalize_long_and_short():
    lp = normalize_position(_P("DU1", _C("NVDA"), 10, 100.0))
    assert isinstance(lp, NormalizedPosition)
    assert lp.symbol == "NVDA" and lp.quantity == 10
    assert lp.quantity_direction == "Long" and lp.instrument_type == "Equity"
    sp = normalize_position(_P("DU1", _C("AMD"), -5, 50.0))
    assert sp.quantity == -5 and sp.quantity_direction == "Short"


def test_normalize_zero_is_flat():
    zp = normalize_position(_P("DU1", _C("F"), 0, 0.0))
    assert zp.quantity_direction == "Flat"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_account.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/ibkr/account.py
"""Account + position access over IBKR, normalized to the shape the kill switch
and dashboard already consume (.symbol/.quantity/.quantity_direction/.instrument_type)."""
from __future__ import annotations

from dataclasses import dataclass

from app.ibkr.gateway import get_gateway

_SECTYPE_TO_INSTRUMENT = {"STK": "Equity"}


@dataclass
class NormalizedPosition:
    symbol: str
    quantity: int
    quantity_direction: str   # "Long" | "Short" | "Flat"
    instrument_type: str      # "Equity" | raw secType
    avg_price: float
    account: str


def normalize_position(p) -> NormalizedPosition:
    qty = int(p.position)
    direction = "Long" if qty > 0 else "Short" if qty < 0 else "Flat"
    sec = getattr(p.contract, "secType", "STK")
    return NormalizedPosition(
        symbol=p.contract.symbol, quantity=qty, quantity_direction=direction,
        instrument_type=_SECTYPE_TO_INSTRUMENT.get(sec, sec),
        avg_price=float(p.avgCost or 0.0), account=getattr(p, "account", ""))


async def get_accounts() -> list[str]:
    ib = await get_gateway().get_ib()
    return list(ib.managedAccounts())


async def get_positions(account: str | None = None) -> list[NormalizedPosition]:
    ib = await get_gateway().get_ib()
    out = [normalize_position(p) for p in ib.positions()]
    if account:
        out = [p for p in out if p.account == account]
    return [p for p in out if p.quantity != 0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_account.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/account.py backend/tests/test_ibkr_account.py
git commit -m "feat(ibkr): accounts + normalized positions

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Live quotes

**Files:**
- Create: `backend/app/ibkr/market.py`
- Test: `backend/tests/test_ibkr_market.py`

Callers expect `get_quotes(symbols) -> dict[symbol, {"bid","ask","last","price"}]` (floats or
None). We normalize an ib_async `Ticker`; the network call is isolated so the normalizer is
unit-tested directly.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_ibkr_market.py
import math
from app.ibkr.market import normalize_ticker


class _T:
    def __init__(self, bid, ask, last, close):
        self.bid, self.ask, self.last, self.close = bid, ask, last, close


def test_normalize_prefers_last_then_mid_then_close():
    q = normalize_ticker(_T(10.0, 10.2, 10.1, 9.9))
    assert q["last"] == 10.1 and q["bid"] == 10.0 and q["ask"] == 10.2
    assert q["price"] == 10.1                       # last wins
    # NaN last -> mid of bid/ask
    q2 = normalize_ticker(_T(10.0, 10.2, math.nan, 9.9))
    assert q2["price"] == 10.1
    # no last/quote -> close
    q3 = normalize_ticker(_T(math.nan, math.nan, math.nan, 9.9))
    assert q3["price"] == 9.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_market.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/ibkr/market.py
"""Live quotes over IBKR (reqMktData). Normalizer kept pure for unit testing."""
from __future__ import annotations

import asyncio
import math

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def normalize_ticker(t) -> dict:
    bid, ask, last, close = _num(t.bid), _num(t.ask), _num(t.last), _num(t.close)
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = last if last is not None else mid if mid is not None else close
    return {"bid": bid, "ask": ask, "last": last, "close": close, "price": price}


async def get_quotes(symbols: list[str], timeout: float = 3.0) -> dict[str, dict]:
    ib = await get_gateway().get_ib()
    tickers = {}
    for sym in symbols:
        tickers[sym] = ib.reqMktData(stock(sym), "", False, False)
    await asyncio.sleep(timeout)   # let ticks arrive
    out = {sym: normalize_ticker(t) for sym, t in tickers.items()}
    for sym in symbols:
        ib.cancelMktData(stock(sym))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_market.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/market.py backend/tests/test_ibkr_market.py
git commit -m "feat(ibkr): live quotes via reqMktData

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Historical provider (forward refresh) + delete Massive

**Files:**
- Create: `backend/app/providers/ibkr_historical.py`
- Modify: `backend/app/providers/__init__.py`
- Modify: `backend/app/providers/base.py` (remove `MassiveNotConfigured`)
- Delete: `backend/app/providers/massive.py`
- Test: `backend/tests/test_ibkr_historical.py`

- [ ] **Step 1: Write the failing test (bar normalization)**

```python
# backend/tests/test_ibkr_historical.py
from datetime import datetime, timezone
from app.providers.ibkr_historical import normalize_bar


class _B:  # ib_async BarData with epoch-seconds date (formatDate=2)
    def __init__(self, t, o, h, l, c, v):
        self.date, self.open, self.high, self.low, self.close, self.volume = t, o, h, l, c, v


def test_normalize_bar_epoch_seconds():
    ts = int(datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc).timestamp())
    b = normalize_bar(_B(ts, 10, 11, 9, 10.5, 1000), "NVDA")
    assert b == {"time": ts * 1000, "open": 10.0, "high": 11.0, "low": 9.0,
                 "close": 10.5, "volume": 1000.0, "symbol": "NVDA"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_historical.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement provider**

```python
# backend/app/providers/ibkr_historical.py
"""IBKR historical bars (forward refresh / scanner only — NOT bulk backfill;
IBKR intraday pacing is strict and depth is limited). Normalized to the canonical
bar shape. Bulk backtests continue to read cached data_cache/*.json."""
from __future__ import annotations

from datetime import date, datetime

from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.providers.base import Bar, HistoricalDataProvider

_TF_TO_IB = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins", "1d": "1 day"}


def normalize_bar(b, symbol: str) -> Bar:
    # formatDate=2 -> b.date is epoch seconds (int) for intraday; for daily it's a date.
    if isinstance(b.date, (int, float)):
        ms = int(b.date) * 1000
    else:
        d = b.date if isinstance(b.date, datetime) else datetime(b.date.year, b.date.month, b.date.day)
        ms = int(d.timestamp() * 1000)
    return {"time": ms, "open": float(b.open), "high": float(b.high), "low": float(b.low),
            "close": float(b.close), "volume": float(b.volume), "symbol": symbol}


class IBKRHistoricalProvider(HistoricalDataProvider):
    name = "ibkr"

    async def get_bars(self, symbol, timeframe, start=None, end=None) -> list[Bar]:
        ib = await get_gateway().get_ib()
        bar_size = _TF_TO_IB[timeframe]
        duration = self._duration(start, timeframe)
        bars = await ib.reqHistoricalDataAsync(
            stock(symbol), endDateTime="", durationStr=duration, barSizeSetting=bar_size,
            whatToShow="TRADES", useRTH=True, formatDate=2)
        return [normalize_bar(b, symbol.upper()) for b in bars]

    @staticmethod
    def _duration(start, timeframe) -> str:
        if start is None:
            return "5 D" if timeframe != "1d" else "1 Y"
        days = max(1, (datetime.now().date() - (start.date() if isinstance(start, datetime) else start)).days)
        return f"{min(days, 30)} D" if timeframe != "1d" else f"{min(days, 365)} D"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_historical.py -v`
Expected: PASS.

- [ ] **Step 5: Rewire factory and delete Massive**

Replace `backend/app/providers/__init__.py` contents with:
```python
"""Provider factory. Historical/forward-refresh data comes from IBKR; bulk
backtests read cached files directly via history_cache."""
from __future__ import annotations

from functools import lru_cache

from app.providers.base import HistoricalDataProvider, ProviderError
from app.providers.ibkr_historical import IBKRHistoricalProvider

__all__ = ["get_historical_provider", "HistoricalDataProvider", "ProviderError"]


@lru_cache
def get_historical_provider() -> HistoricalDataProvider:
    return IBKRHistoricalProvider()
```
In `backend/app/providers/base.py`, delete the `MassiveNotConfigured` class.
Delete the file: `rm backend/app/providers/massive.py`.

- [ ] **Step 6: Verify nothing imports the removed names**

Run: `grep -rn "massive\|MassiveNotConfigured\|MassiveHistoricalProvider" backend/app | grep -v __pycache__`
Expected: no matches in `app/` (matches under `scripts/` referencing cached `source="massive"` strings are fine — those are data filenames, not imports). Fix any code imports found.

- [ ] **Step 7: Run provider tests + commit**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_historical.py -v`
Expected: PASS.
```bash
git add backend/app/providers backend/tests/test_ibkr_historical.py
git commit -m "feat(providers): IBKR historical provider; remove Massive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Execution rewrite — pure mapping + bracket builder

**Files:**
- Create: `backend/app/engine/ibkr_orders.py` (pure order construction)
- Test: `backend/tests/test_ibkr_orders.py`

Isolate the pure order-construction logic (testable without a Gateway) from the async
transmit path (Task 9).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_ibkr_orders.py
from app.engine.ibkr_orders import intent_to_action, build_bracket


def test_intent_to_action():
    assert intent_to_action("enter_long") == "BUY"
    assert intent_to_action("exit_short") == "BUY"
    assert intent_to_action("exit_long") == "SELL"
    assert intent_to_action("enter_short") == "SELL"


def test_build_bracket_long_market_parent_oco_children():
    parent, tp, stop = build_bracket("long", 10, target=110.0, stop=95.0)
    assert parent.action == "BUY" and parent.orderType == "MKT" and parent.totalQuantity == 10
    assert parent.transmit is False
    assert tp.action == "SELL" and tp.orderType == "LMT" and tp.lmtPrice == 110.0
    assert tp.parentId == parent.orderId and tp.transmit is False
    assert stop.action == "SELL" and stop.orderType == "STP" and stop.auxPrice == 95.0
    assert stop.parentId == parent.orderId and stop.transmit is True   # last leg transmits


def test_build_bracket_short_reverses_actions():
    parent, tp, stop = build_bracket("short", 4, target=90.0, stop=105.0)
    assert parent.action == "SELL"
    assert tp.action == "BUY" and stop.action == "BUY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_orders.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/engine/ibkr_orders.py
"""Pure IBKR order construction (no network): intent->action and bracket assembly.

A bracket is a MARKET parent (transmit=False) plus an OCO pair — take-profit LIMIT
and stop STP — that share the parent's id; the last child carries transmit=True so
IBKR releases the whole group atomically. Order ids are assigned by the caller
(the live path) via ib.client.getReqId(); here they default to placeholders so the
structure is unit-testable."""
from __future__ import annotations

from ib_async import LimitOrder, MarketOrder, StopOrder

_INTENT_TO_ACTION = {
    "enter_long": "BUY", "exit_short": "BUY",
    "exit_long": "SELL", "enter_short": "SELL",
}


def intent_to_action(intent: str) -> str:
    try:
        return _INTENT_TO_ACTION[intent]
    except KeyError:
        raise ValueError(f"Unknown order intent: {intent}")


def build_bracket(side: str, quantity: int, *, target: float, stop: float,
                  parent_id: int = 0, tp_id: int = 0, stop_id: int = 0):
    if side == "long":
        entry_action, close_action = "BUY", "SELL"
    elif side == "short":
        entry_action, close_action = "SELL", "BUY"
    else:
        raise ValueError(f"Unknown side: {side!r}")

    parent = MarketOrder(entry_action, quantity)
    parent.orderId = parent_id
    parent.transmit = False

    tp = LimitOrder(close_action, quantity, target)
    tp.orderId = tp_id
    tp.parentId = parent_id
    tp.transmit = False

    stp = StopOrder(close_action, quantity, stop)
    stp.orderId = stop_id
    stp.parentId = parent_id
    stp.transmit = True
    return parent, tp, stp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_ibkr_orders.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/ibkr_orders.py backend/tests/test_ibkr_orders.py
git commit -m "feat(engine): pure IBKR order construction + bracket builder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Execution rewrite — transmit path (same public API)

**Files:**
- Modify: `backend/app/engine/execution.py` (full rewrite)
- Test: `backend/tests/test_execution_ibkr.py`

Preserve the public functions used by callers: `submit_equity_order`, `submit_bracket_order`,
`cancel_symbol_orders`, `poll_bracket_fill`, `flatten_all`. Each respects `ENABLE_LIVE_ORDERS`
(off = log + return `transmitted: False`).

- [ ] **Step 1: Write the failing test (paper-mode gating, no Gateway)**

```python
# backend/tests/test_execution_ibkr.py
import pytest
from app.engine import execution
from app.config import get_settings


@pytest.mark.asyncio
async def test_equity_order_paper_mode_does_not_transmit(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    res = await execution.submit_equity_order("NVDA", "enter_long", 10, reason="test")
    assert res["transmitted"] is False and res.get("paper") is True


@pytest.mark.asyncio
async def test_bracket_paper_mode_returns_levels(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
    get_settings.cache_clear()
    res = await execution.submit_bracket_order("NVDA", "long", 10, 100.0, 95.0, 110.0)
    assert res["transmitted"] is False and res["stop"] == 95.0 and res["target"] == 110.0


@pytest.mark.asyncio
async def test_zero_quantity_skips():
    res = await execution.submit_equity_order("NVDA", "enter_long", 0)
    assert res["transmitted"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_execution_ibkr.py -v`
Expected: FAIL (execution still imports tastytrade / signatures differ).

- [ ] **Step 3: Rewrite `execution.py`**

```python
# backend/app/engine/execution.py
"""Order execution + flatten over IBKR (ib_async).

ENABLE_LIVE_ORDERS gates transmission: off = log + return transmitted:False (the
worker still records the paper trade); on = transmit to the IBKR paper account.
flatten_all stays honest: it raises if any real exposure remains."""
from __future__ import annotations

import logging

from app.config import get_settings
from app.engine.ibkr_orders import build_bracket, intent_to_action
from app.ibkr.account import get_positions
from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from ib_async import MarketOrder

logger = logging.getLogger("trading.execution")


async def submit_equity_order(symbol: str, intent: str, quantity: int, *, reason: str = "") -> dict:
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    action = intent_to_action(intent)
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] %s %s x%d (%s) — ENABLE_LIVE_ORDERS off", action, symbol, quantity, reason)
        return {"transmitted": False, "paper": True, "detail": f"paper {action} {symbol} x{quantity}"}
    ib = await get_gateway().get_ib()
    contract = stock(symbol)
    await ib.qualifyContractsAsync(contract)
    trade = ib.placeOrder(contract, MarketOrder(action, quantity))
    logger.warning("[LIVE] %s %s x%d (%s)", action, symbol, quantity, reason)
    return {"transmitted": True, "detail": f"LIVE {action} {symbol} x{quantity}",
            "order_id": trade.order.orderId}


async def submit_bracket_order(symbol, side, quantity, entry_price, stop, target, *, reason="") -> dict:
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    if side not in ("long", "short"):
        raise ValueError(f"Unknown side: {side!r}")
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] BRACKET %s %s x%d stop %.2f target %.2f (%s) — off",
                       side, symbol, quantity, stop, target, reason)
        return {"transmitted": False, "paper": True, "bracket": True,
                "detail": f"paper bracket {side} {symbol} x{quantity}", "stop": stop, "target": target}
    ib = await get_gateway().get_ib()
    contract = stock(symbol)
    await ib.qualifyContractsAsync(contract)
    parent_id, tp_id, stop_id = ib.client.getReqId(), ib.client.getReqId(), ib.client.getReqId()
    parent, tp, stp = build_bracket(side, quantity, target=target, stop=stop,
                                    parent_id=parent_id, tp_id=tp_id, stop_id=stop_id)
    for order in (parent, tp, stp):
        ib.placeOrder(contract, order)
    logger.warning("[LIVE BRACKET] %s %s x%d stop %.2f target %.2f (%s)",
                   side, symbol, quantity, stop, target, reason)
    return {"transmitted": True, "bracket": True, "detail": f"LIVE bracket {side} {symbol} x{quantity}",
            "parent_order_id": parent_id, "stop": stop, "target": target}


async def cancel_symbol_orders(symbol: str) -> dict:
    s = get_settings()
    if not s.enable_live_orders:
        return {"cancelled": 0, "paper": True}
    ib = await get_gateway().get_ib()
    cancelled = 0
    for trade in ib.openTrades():
        if getattr(trade.contract, "symbol", None) == symbol:
            ib.cancelOrder(trade.order)
            cancelled += 1
    return {"cancelled": cancelled}


async def poll_bracket_fill(symbol: str, ref: str | int | None = None) -> dict | None:
    """None while still holding symbol; else {price, ms, reason} for the closed bracket."""
    s = get_settings()
    if not s.enable_live_orders:
        return None
    ib = await get_gateway().get_ib()
    for p in await get_positions():
        if p.symbol == symbol and p.quantity != 0:
            return None  # still open
    # Flat -> find the most recent filled closing child for this symbol.
    for trade in reversed(ib.trades()):
        if getattr(trade.contract, "symbol", None) != symbol:
            continue
        if trade.orderStatus.status != "Filled":
            continue
        otype = trade.order.orderType
        if otype == "STP":
            return {"price": float(trade.order.auxPrice or 0), "ms": None, "reason": "stop"}
        if otype == "LMT":
            return {"price": float(trade.order.lmtPrice or 0), "ms": None, "reason": "target"}
    return {"price": None, "ms": None, "reason": "bracket"}


async def flatten_all() -> str:
    if not get_gateway().is_connected():
        return "Not connected to IBKR — nothing to flatten."
    s = get_settings()
    ib = await get_gateway().get_ib()
    positions = await get_positions()
    cancelled = 0
    closed: list[str] = []
    failures: list[str] = []

    if s.enable_live_orders:
        for trade in ib.openTrades():
            try:
                ib.cancelOrder(trade.order); cancelled += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"order-cancel {getattr(trade.contract,'symbol','?')}: {exc}")

    for p in positions:
        qty = abs(p.quantity)
        if qty <= 0:
            continue
        if p.instrument_type != "Equity":
            failures.append(f"{p.symbol} ({p.instrument_type}: cannot auto-flatten)"); continue
        action = "SELL" if p.quantity > 0 else "BUY"
        if not s.enable_live_orders:
            logger.warning("[FLATTEN-PAPER] would %s %s x%d", action, p.symbol, qty)
            failures.append(f"{p.symbol} x{qty} (paper: not transmitted)"); continue
        try:
            c = stock(p.symbol); await ib.qualifyContractsAsync(c)
            ib.placeOrder(c, MarketOrder(action, qty))
            closed.append(f"{p.symbol} x{qty}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{p.symbol} ({exc})")

    note = f"cancelled {cancelled} resting order(s); " if cancelled else ""
    total = len([p for p in positions if abs(p.quantity) > 0])
    if total == 0 and not failures:
        return f"{note}No open positions." if note else "No open positions."
    if failures:
        raise RuntimeError(f"Flatten incomplete: {note}closed {len(closed)}/{total} "
                           f"[{', '.join(closed) or 'none'}]; not flat: {', '.join(failures)}")
    return f"{note}Flattened {total} position(s): {', '.join(closed)}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_execution_ibkr.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Check callers for the renamed bracket key / poll signature**

The rewrite renames the bracket return key (`complex_order_id` → `parent_order_id`) and
`poll_bracket_fill` now takes `(symbol, ref=None)`. Find consumers:

Run: `grep -rn "complex_order_id\|poll_bracket_fill\|submit_bracket_order" backend/app | grep -v __pycache__`
Expected: usually the strategy worker (e.g. `app/engine/worker.py`). For each hit, update any
`res["complex_order_id"]` read to `res.get("parent_order_id")`, and confirm `poll_bracket_fill`
calls still pass the (now ignored) id positionally — they remain compatible. Re-run the grep
until only definitions remain.

- [ ] **Step 6: Commit**

```bash
git add backend/app/engine/execution.py backend/tests/test_execution_ibkr.py
git commit -m "feat(engine): rewrite execution against IBKR (same public API)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Auth — replace OAuth with IBKR connection state

**Files:**
- Modify: `backend/app/auth/manager.py` (replace internals)
- Modify: `backend/app/auth/routes.py` (connect/disconnect endpoints)
- Modify: `backend/app/auth/gate.py` (gate on connection, not cookie)
- Test: `backend/tests/test_auth_connection.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_auth_connection.py
import pytest
from app.auth.manager import session_manager


@pytest.mark.asyncio
async def test_is_authenticated_tracks_gateway(monkeypatch):
    from app.ibkr.gateway import get_gateway
    monkeypatch.setattr(get_gateway(), "is_connected", lambda: False)
    assert session_manager.is_authenticated() is False
    monkeypatch.setattr(get_gateway(), "is_connected", lambda: True)
    assert session_manager.is_authenticated() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_auth_connection.py -v`
Expected: FAIL (manager still OAuth-based).

- [ ] **Step 3: Replace `manager.py` with a connection-backed shim**

```python
# backend/app/auth/manager.py
"""Auth = IBKR Gateway connection state (no OAuth, no per-user secrets).

Kept as a 'session_manager' singleton so existing callers (kill switch, gate)
keep working; is_authenticated() now means 'Gateway connected'."""
from __future__ import annotations

from app.ibkr.gateway import get_gateway


class SessionManager:
    def is_authenticated(self) -> bool:
        return get_gateway().is_connected()

    async def connect(self):
        await get_gateway().connect()
        return self.is_authenticated()

    def disconnect(self):
        get_gateway().disconnect()


session_manager = SessionManager()
```

- [ ] **Step 4: Replace `routes.py` auth endpoints**

```python
# backend/app/auth/routes.py
"""Connection endpoints (replaces OAuth login/callback)."""
from __future__ import annotations

from fastapi import APIRouter

from app.auth.manager import session_manager
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
async def status():
    s = get_settings()
    return {"connected": session_manager.is_authenticated(),
            "host": s.ibkr_host, "port": s.ibkr_port, "account": s.ibkr_account}


@router.post("/connect")
async def connect():
    ok = await session_manager.connect()
    return {"connected": ok}


@router.post("/disconnect")
async def disconnect():
    session_manager.disconnect()
    return {"connected": False}
```

- [ ] **Step 5: Simplify `gate.py` to use connection state**

Open `backend/app/auth/gate.py`. Replace any cookie/OAuth validation dependency with a check
on `session_manager.is_authenticated()`. If the gate currently raises 401 when not
authenticated, keep that behavior but source it from `session_manager.is_authenticated()`.
Remove imports of `store`/cookie helpers. (Per project memory there is no hard app gate on the
dashboard; ensure read-only dashboard routes remain accessible and only order/trade actions
require connection.)

- [ ] **Step 6: Run test + commit**

Run: `../.venv/bin/python -m pytest tests/test_auth_connection.py -v`
Expected: PASS.
```bash
git add backend/app/auth backend/tests/test_auth_connection.py
git commit -m "feat(auth): connection-state auth via IBKR Gateway (drop OAuth)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: Live stream + market service rewire

**Files:**
- Modify: `backend/app/services/market.py`
- Modify: `backend/app/services/market_stream.py`
- Modify: `backend/app/services/__init__.py` (if it re-exports tastytrade)

- [ ] **Step 1: Point `market.py` quote calls at IBKR**

In `backend/app/services/market.py`, replace any `from app.tastytrade.client import get_client`
and `client.get_quotes(...)` usage with:
```python
from app.ibkr.market import get_quotes
# ... wherever quotes were fetched:
quotes = await get_quotes(symbols)
```
Keep the function names/return shapes the routers already expect (the IBKR quote dict has
`bid/ask/last/close/price`).

- [ ] **Step 2: Rewire `market_stream.py` to ib_async tickers**

Replace the DXLink streaming loop with ib_async live tickers:
```python
# backend/app/services/market_stream.py  (core of the stream)
from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway
from app.ibkr.market import normalize_ticker


async def stream_quotes(symbols, on_quote):
    """Subscribe to live tickers and call on_quote(symbol, quote_dict) on updates."""
    ib = await get_gateway().get_ib()
    tickers = {ib.reqMktData(stock(s), "", False, False): s for s in symbols}

    def _handler(ticker):
        sym = tickers.get(ticker)
        if sym:
            on_quote(sym, normalize_ticker(ticker))

    ib.pendingTickersEvent += lambda ts: [_handler(t) for t in ts]
```
Adapt the surrounding lifecycle (start/stop, the websocket fan-out in `routers/ws.py`) to call
`stream_quotes`. Remove DXLink/Tastytrade streamer code.

- [ ] **Step 3: Verify imports resolve**

Run: `../.venv/bin/python -c "import app.services.market, app.services.market_stream; print('ok')"`
Expected: prints `ok` (no tastytrade import errors).

- [ ] **Step 4: Commit**

```bash
git add backend/app/services
git commit -m "feat(services): live quotes/stream over IBKR

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 12: Delete Tastytrade code + purge references

**Files:**
- Delete: `backend/app/tastytrade/` (whole dir)
- Delete: `backend/app/providers/tastytrade_live.py`
- Delete: `backend/app/auth/store.py` (OAuth token store) if unused after Task 10
- Modify: any remaining importers surfaced by grep

- [ ] **Step 1: Find all references**

Run: `grep -rn "tastytrade\|TastytradeClient\|get_client\|dxlink\|DXLink\|provider_secret\|refresh_token" backend/app | grep -v __pycache__`
Expected: a list — every hit must be removed or rewired.

- [ ] **Step 2: Delete the modules**

```bash
rm -rf backend/app/tastytrade backend/app/providers/tastytrade_live.py
```
Remove `app/auth/store.py` only if Task 10 left it unused (re-grep `from app.auth import store`).

- [ ] **Step 3: Fix each remaining importer**

For every file in the Step-1 list, remove the tastytrade import and replace the call with the
IBKR equivalent (`app.ibkr.market.get_quotes`, `app.ibkr.account.get_positions/get_accounts`,
`app.engine.execution.*`). Re-run the grep until it returns nothing under `app/`.

- [ ] **Step 4: Verify the app imports cleanly**

Run: `../.venv/bin/python -c "import app.main; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add -A backend/app
git commit -m "chore: remove Tastytrade modules and references

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 13: Frontend — IBKR connection tab

**Files:**
- Modify: the API/Connection tab component under `frontend/` (locate with grep below)

- [ ] **Step 1: Locate the current Tastytrade auth UI**

Run: `grep -rln "tastytrade\|OAuth\|refresh token\|provider secret\|Massive\|API key" frontend/src`
Expected: the API-tab component(s). Open them.

- [ ] **Step 2: Replace credential fields with connection config + status**

In the API-tab component, remove Tastytrade OAuth / Massive API-key inputs. Add: read-only
display of `host`/`port`/`account` from `GET /api/auth/status`, a **Connected/Disconnected**
badge, and **Connect**/**Disconnect** buttons calling `POST /api/auth/connect` /
`POST /api/auth/disconnect`. Match the existing dark dashboard / indigo-accent style. Poll
`/api/auth/status` every few seconds to keep the badge live.

- [ ] **Step 3: Update the dashboard auth indicator**

Replace any "logged in to Tastytrade" indicator with the connection badge sourced from
`/api/auth/status`.

- [ ] **Step 4: Build the frontend to verify it compiles**

Run: `cd frontend && npm run build`
Expected: build succeeds with no references to removed fields.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): IBKR Gateway connection tab + status badge

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 14: Connection smoke script (user-run live validation)

**Files:**
- Create: `backend/scripts/connect_smoke.py`

- [ ] **Step 1: Implement the script**

```python
# backend/scripts/connect_smoke.py
"""Manual live validation against a running IB Gateway (paper). NOT a unit test.

Usage (with IB Gateway running + logged into the paper account):
    ../.venv/bin/python -m scripts.connect_smoke
    ENABLE_LIVE_ORDERS=1 ../.venv/bin/python -m scripts.connect_smoke --bracket NVDA
"""
from __future__ import annotations

import argparse
import asyncio

from app.ibkr.account import get_accounts, get_positions
from app.ibkr.gateway import get_gateway
from app.ibkr.market import get_quotes
from app.engine.execution import submit_bracket_order, flatten_all


async def main(bracket_symbol: str | None):
    await get_gateway().connect()
    print("accounts:", await get_accounts())
    print("NVDA quote:", (await get_quotes(["NVDA"]))["NVDA"])
    print("positions:", await get_positions())
    if bracket_symbol:
        q = (await get_quotes([bracket_symbol]))[bracket_symbol]["price"]
        res = await submit_bracket_order(bracket_symbol, "long", 1, q, q * 0.98, q * 1.03,
                                         reason="smoke")
        print("bracket:", res)
        input("Press Enter to flatten...")
        print("flatten:", await flatten_all())
    get_gateway().disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bracket", default=None, help="symbol for a 1-share paper bracket test")
    args = ap.parse_args()
    asyncio.run(main(args.bracket))
```

- [ ] **Step 2: Commit**

```bash
git add backend/scripts/connect_smoke.py
git commit -m "feat(scripts): IBKR connection smoke test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 15: Full verification

- [ ] **Step 1: Run the entire backend test suite**

Run: `cd backend && ../.venv/bin/python -m pytest -q`
Expected: all tests pass. Investigate and fix any failure (likely a stale tastytrade/massive
import in a test — update or delete that test).

- [ ] **Step 2: Confirm no stale backend references remain**

Run: `grep -rn "tastytrade\|MassiveHistoricalProvider\|MassiveNotConfigured\|dxlink" backend/app | grep -v __pycache__`
Expected: no matches.

- [ ] **Step 3: Confirm the app boots**

Run: `../.venv/bin/python -c "import app.main; print('app imports ok')"`
Expected: prints `app imports ok`.

- [ ] **Step 4: Manual live check (user, with Gateway running)**

Run: `../.venv/bin/python -m scripts.connect_smoke`
Expected: prints the paper account id(s), a live NVDA quote, and current positions. Then, with
`ENABLE_LIVE_ORDERS=1 ... --bracket NVDA`, a 1-share paper bracket is placed and flattened.

- [ ] **Step 5: Final commit**

```bash
git add -A && git commit -m "test: full suite green after IBKR migration

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
