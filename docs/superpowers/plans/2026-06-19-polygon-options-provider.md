# Polygon Options Data Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an options market-data layer backed by Polygon (chains + historical bars) behind a clean `app/options/` seam, with tests. Foundation only — no consumer. Equities stay on IBKR.

**Architecture:** A standalone `app/options/` package: pure OCC-ticker helpers, an `OptionsDataProvider` ABC + normalized types/errors, and a `PolygonOptionsProvider` (async httpx, pagination + 429 backoff) calling Polygon's `/v3/reference/options/contracts` and `/v2/aggs/...`. Pure logic is TDD'd; live HTTP is validated by a smoke script.

**Tech Stack:** Python 3.14, httpx (already a dep), pydantic-settings, pytest.

**Spec:** `docs/superpowers/specs/2026-06-19-polygon-options-provider-design.md`

> Run all `pytest` from `backend/`. Python: `/Users/karanpatel/PycharmProjects/Tradding-system/.venv/bin/python`. Repo under git; commit messages end with the Co-Authored-By trailer.

---

### Task 1: Config — Polygon settings

**Files:**
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_config_polygon.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_config_polygon.py
from app.config import Settings


def test_polygon_base_url_default():
    assert Settings().polygon_base_url == "https://api.polygon.io"


def test_polygon_key_from_POLYGON_API_KEY(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "pk_test")
    assert Settings().polygon_api_key == "pk_test"


def test_polygon_key_falls_back_to_MASSIVE_API_KEY(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.setenv("MASSIVE_API_KEY", "legacy_key")
    assert Settings().polygon_api_key == "legacy_key"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_config_polygon.py -v`
Expected: FAIL (no `polygon_base_url`/`polygon_api_key`).

- [ ] **Step 3: Add settings**

In `backend/app/config.py`, ensure the imports include `Field` and `AliasChoices`:
```python
from pydantic import Field
from pydantic_settings import AliasChoices, BaseSettings, SettingsConfigDict
```
(If `from pydantic import ...` / `AliasChoices` are not already imported, add them. `AliasChoices` lives in `pydantic`, so `from pydantic import AliasChoices, Field` also works — use whichever import path resolves; verify with `python -c "from pydantic import AliasChoices, Field"`.)

Then add these fields just after the `frontend_dist` field:
```python
    # Polygon — OPTIONS market data only (stocks come from IBKR). Reuses the existing
    # MASSIVE_API_KEY value already in backend/.env unless POLYGON_API_KEY is set.
    polygon_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("POLYGON_API_KEY", "MASSIVE_API_KEY"))
    polygon_base_url: str = "https://api.polygon.io"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_config_polygon.py -v`
Expected: PASS. Also run `../.venv/bin/python -c "import app.main; print('ok')"` (catches a bad import).

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_config_polygon.py
git commit -m "feat(config): add Polygon options settings (key + base url)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: OCC ticker helpers

**Files:**
- Create: `backend/app/options/__init__.py` (empty for now)
- Create: `backend/app/options/contracts.py`
- Test: `backend/tests/test_occ_ticker.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_occ_ticker.py
from datetime import date

from app.options.contracts import occ_ticker, parse_occ


def test_occ_ticker_call():
    assert occ_ticker("SPY", date(2026, 6, 22), "C", 747.0) == "O:SPY260622C00747000"


def test_occ_ticker_put_and_fractional_strike():
    assert occ_ticker("F", date(2025, 1, 17), "P", 7.5) == "O:F250117P00007500"


def test_parse_occ_round_trip():
    t = "O:SPY260622C00747000"
    p = parse_occ(t)
    assert p == {"underlying": "SPY", "expiry": date(2026, 6, 22), "right": "C", "strike": 747.0}
    assert occ_ticker(p["underlying"], p["expiry"], p["right"], p["strike"]) == t
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_occ_ticker.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/options/contracts.py
"""Polygon OCC option-ticker helpers (pure).

Format: ``O:<UNDERLYING><YYMMDD><C|P><strike*1000, 8 digits>``
e.g. SPY 2026-06-22 $747 call -> ``O:SPY260622C00747000``."""
from __future__ import annotations

from datetime import date


def occ_ticker(underlying: str, expiry: date, right: str, strike: float) -> str:
    strike_int = int(round(strike * 1000))
    return f"O:{underlying.upper()}{expiry:%y%m%d}{right.upper()}{strike_int:08d}"


def parse_occ(ticker: str) -> dict:
    body = ticker[2:] if ticker.startswith("O:") else ticker
    tail = body[-15:]          # 6 date + 1 right + 8 strike
    underlying = body[:-15]
    expiry = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
    right = tail[6]
    strike = int(tail[7:]) / 1000.0
    return {"underlying": underlying, "expiry": expiry, "right": right, "strike": strike}
```
Also create empty `backend/app/options/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_occ_ticker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/__init__.py backend/app/options/contracts.py backend/tests/test_occ_ticker.py
git commit -m "feat(options): OCC option-ticker build/parse helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Provider interface, types, and errors

**Files:**
- Create: `backend/app/options/base.py`
- Test: `backend/tests/test_options_base.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_options_base.py
from datetime import date

from app.options.base import (OptionContract, OptionsProviderError,
                              PolygonNotAuthorized, PolygonNotConfigured)


def test_option_contract_fields():
    c = OptionContract(ticker="O:SPY260622C00747000", underlying="SPY",
                       expiration=date(2026, 6, 22), strike=747.0, right="C",
                       shares_per_contract=100)
    assert c.underlying == "SPY" and c.right == "C" and c.strike == 747.0


def test_error_hierarchy():
    assert issubclass(PolygonNotConfigured, OptionsProviderError)
    assert issubclass(PolygonNotAuthorized, OptionsProviderError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_options_base.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/options/base.py
"""Options data-provider abstraction: normalized types + errors + the interface.
Bars reuse the canonical shape used elsewhere:
``{time(ms,UTC), open, high, low, close, volume, symbol}`` (symbol = option ticker)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime

Bar = dict


class OptionsProviderError(RuntimeError):
    """Base class for options-provider failures."""


class PolygonNotConfigured(OptionsProviderError):
    """No Polygon API key configured."""


class PolygonNotAuthorized(OptionsProviderError):
    """Polygon 403 — plan doesn't include the requested data/timeframe."""


@dataclass
class OptionContract:
    ticker: str
    underlying: str
    expiration: date
    strike: float
    right: str               # "C" | "P"
    shares_per_contract: int


class OptionsDataProvider(ABC):
    name: str = "options"

    @abstractmethod
    async def list_contracts(self, underlying: str, *, expired: bool = False,
                             expiration_date: str | None = None,
                             contract_type: str | None = None,
                             limit: int = 1000) -> list[OptionContract]:
        raise NotImplementedError

    @abstractmethod
    async def get_aggregates(self, option_ticker: str, timeframe: str,
                             start: date | datetime, end: date | datetime) -> list[Bar]:
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_options_base.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/base.py backend/tests/test_options_base.py
git commit -m "feat(options): OptionsDataProvider interface, types, errors

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Polygon provider — pure helpers (TDD)

**Files:**
- Create: `backend/app/options/polygon.py` (pure helpers first; async methods in Task 5)
- Test: `backend/tests/test_polygon_helpers.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_polygon_helpers.py
import pytest

from app.options.base import OptionContract, PolygonNotAuthorized
from app.options.polygon import (_agg_path, _check_status, _normalize_bar,
                                 _normalize_contract, _tf)


def test_tf_map():
    assert _tf("1m") == (1, "minute")
    assert _tf("1d") == (1, "day")
    with pytest.raises(ValueError):
        _tf("2w")


def test_agg_path():
    from datetime import date
    p = _agg_path("O:SPY260622C00747000", "1d", date(2024, 1, 1), date(2024, 6, 30))
    assert p == "/v2/aggs/ticker/O:SPY260622C00747000/range/1/day/2024-01-01/2024-06-30"


def test_normalize_bar():
    r = {"t": 1718971800000, "o": 1.0, "h": 1.5, "l": 0.9, "c": 1.2, "v": 345}
    assert _normalize_bar(r, "O:SPY260622C00747000") == {
        "time": 1718971800000, "open": 1.0, "high": 1.5, "low": 0.9,
        "close": 1.2, "volume": 345.0, "symbol": "O:SPY260622C00747000"}


def test_normalize_contract():
    r = {"ticker": "O:SPY260622C00747000", "underlying_ticker": "SPY",
         "expiration_date": "2026-06-22", "strike_price": 747.0,
         "contract_type": "call", "shares_per_contract": 100}
    c = _normalize_contract(r)
    assert isinstance(c, OptionContract) and c.right == "C" and c.underlying == "SPY"


def test_check_status_403_not_authorized():
    with pytest.raises(PolygonNotAuthorized):
        _check_status(403, {"status": "NOT_AUTHORIZED", "message": "upgrade plan"})
    # 200 is fine (no raise)
    _check_status(200, {"status": "OK"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_polygon_helpers.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the pure helpers**

```python
# backend/app/options/polygon.py
"""Polygon options provider: pure helpers (this task) + async client (next task)."""
from __future__ import annotations

from datetime import date, datetime

from app.options.base import (Bar, OptionContract, OptionsProviderError,
                              PolygonNotAuthorized)

_TF = {"1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
       "1h": (1, "hour"), "1d": (1, "day")}


def _tf(timeframe: str) -> tuple[int, str]:
    try:
        return _TF[timeframe]
    except KeyError:
        raise ValueError(f"Unsupported timeframe: {timeframe}")


def _fmt(d: date | datetime) -> str:
    return (d.date() if isinstance(d, datetime) else d).isoformat()


def _agg_path(ticker: str, timeframe: str, start, end) -> str:
    mult, span = _tf(timeframe)
    return f"/v2/aggs/ticker/{ticker}/range/{mult}/{span}/{_fmt(start)}/{_fmt(end)}"


def _normalize_bar(r: dict, ticker: str) -> Bar:
    return {"time": int(r["t"]), "open": float(r["o"]), "high": float(r["h"]),
            "low": float(r["l"]), "close": float(r["c"]),
            "volume": float(r.get("v", 0.0)), "symbol": ticker}


def _normalize_contract(r: dict) -> OptionContract:
    return OptionContract(
        ticker=r["ticker"], underlying=r["underlying_ticker"],
        expiration=date.fromisoformat(r["expiration_date"]),
        strike=float(r["strike_price"]),
        right="C" if r["contract_type"] == "call" else "P",
        shares_per_contract=int(r.get("shares_per_contract", 100)))


def _check_status(status_code: int, body: dict | None) -> None:
    body = body or {}
    if status_code == 403 and body.get("status") == "NOT_AUTHORIZED":
        raise PolygonNotAuthorized(body.get("message", "Polygon plan not authorized"))
    if status_code >= 400:
        raise OptionsProviderError(f"Polygon HTTP {status_code}: {body.get('message', body)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_polygon_helpers.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/polygon.py backend/tests/test_polygon_helpers.py
git commit -m "feat(options): Polygon pure helpers (tf map, paths, normalization, 403)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Polygon provider — async client + factory

**Files:**
- Modify: `backend/app/options/polygon.py` (add the class + `_get`)
- Modify: `backend/app/options/__init__.py` (factory)
- Test: `backend/tests/test_options_factory.py`

- [ ] **Step 1: Write the failing test (factory gating, no network)**

```python
# backend/tests/test_options_factory.py
import pytest

from app.options import get_options_provider
from app.options.base import PolygonNotConfigured


def test_factory_raises_without_key(monkeypatch):
    from app import config
    config.get_settings.cache_clear()
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    config.get_settings.cache_clear()
    with pytest.raises(PolygonNotConfigured):
        get_options_provider()
    config.get_settings.cache_clear()


def test_factory_builds_with_key(monkeypatch):
    from app import config
    monkeypatch.setenv("POLYGON_API_KEY", "pk_test")
    config.get_settings.cache_clear()
    p = get_options_provider()
    assert p.name == "polygon-options"
    config.get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_options_factory.py -v`
Expected: FAIL (`get_options_provider`/class missing).

- [ ] **Step 3: Append the async client to `polygon.py`**

Add to the end of `backend/app/options/polygon.py`:
```python
import asyncio

import httpx

from app.options.base import OptionsDataProvider


class PolygonOptionsProvider(OptionsDataProvider):
    name = "polygon-options"

    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io",
                 *, max_retries: int = 5, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout

    async def _get(self, url: str, params: dict | None = None) -> dict:
        full = url if url.startswith("http") else f"{self.base_url}{url}"
        p = dict(params or {})
        p["apiKey"] = self.api_key
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            backoff = 1.0
            for _ in range(self.max_retries):
                r = await client.get(full, params=p)
                if r.status_code == 429:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                try:
                    body = r.json()
                except Exception:  # noqa: BLE001
                    body = {}
                _check_status(r.status_code, body)
                return body
            raise OptionsProviderError("Polygon rate limit: retries exhausted")

    async def list_contracts(self, underlying, *, expired=False, expiration_date=None,
                             contract_type=None, limit=1000) -> list[OptionContract]:
        params = {"underlying_ticker": underlying.upper(), "expired": str(expired).lower(),
                  "limit": min(limit, 1000)}
        if expiration_date:
            params["expiration_date"] = expiration_date
        if contract_type:
            params["contract_type"] = contract_type
        out: list[OptionContract] = []
        body = await self._get("/v3/reference/options/contracts", params)
        while True:
            out.extend(_normalize_contract(r) for r in body.get("results", []))
            if len(out) >= limit or not body.get("next_url"):
                break
            body = await self._get(body["next_url"])
        return out[:limit]

    async def get_aggregates(self, option_ticker, timeframe, start, end) -> list[Bar]:
        path = _agg_path(option_ticker, timeframe, start, end)
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}
        out: list[Bar] = []
        body = await self._get(path, params)
        while True:
            out.extend(_normalize_bar(r, option_ticker) for r in body.get("results", []))
            if not body.get("next_url"):
                break
            body = await self._get(body["next_url"])
        return out
```

- [ ] **Step 4: Implement the factory in `__init__.py`**

Replace `backend/app/options/__init__.py` with:
```python
"""Options data provider factory (Polygon)."""
from __future__ import annotations

from app.config import get_settings
from app.options.base import OptionsDataProvider, PolygonNotConfigured
from app.options.polygon import PolygonOptionsProvider

__all__ = ["get_options_provider", "OptionsDataProvider", "PolygonNotConfigured"]


def get_options_provider() -> OptionsDataProvider:
    s = get_settings()
    if not s.polygon_api_key:
        raise PolygonNotConfigured("POLYGON_API_KEY (or MASSIVE_API_KEY) is not set.")
    return PolygonOptionsProvider(s.polygon_api_key, s.polygon_base_url)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_options_factory.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/options/polygon.py backend/app/options/__init__.py backend/tests/test_options_factory.py
git commit -m "feat(options): PolygonOptionsProvider async client + factory

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Live smoke script

**Files:**
- Create: `backend/scripts/polygon_options_smoke.py`

- [ ] **Step 1: Implement the script**

```python
# backend/scripts/polygon_options_smoke.py
"""Manual live check of the Polygon options provider (uses the configured key).
NOT a unit test.

    ../.venv/bin/python -m scripts.polygon_options_smoke
"""
from __future__ import annotations

import asyncio
from datetime import date

from app.options import get_options_provider
from app.options.base import PolygonNotAuthorized


async def main() -> int:
    p = get_options_provider()
    chain = await p.list_contracts("SPY", expired=False, contract_type="call", limit=50)
    print(f"SPY current call contracts: {len(chain)} (sample: {chain[0].ticker if chain else 'none'})")
    if chain:
        c = chain[len(chain) // 2]
        try:
            bars = await p.get_aggregates(c.ticker, "1d", date(2024, 1, 1), c.expiration)
            print(f"{c.ticker}: {len(bars)} daily bars"
                  + (f", earliest {bars[0]['time']}" if bars else ""))
        except PolygonNotAuthorized as e:
            print(f"{c.ticker}: NOT AUTHORIZED for that range — {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run it live**

Run: `cd backend && ../.venv/bin/python -m scripts.polygon_options_smoke`
Expected: prints a non-zero SPY contract count and either a daily-bar count or a clean
`NOT AUTHORIZED` message for out-of-plan ranges (both are valid — they prove the provider
works and surfaces the plan limit as a typed error).

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/polygon_options_smoke.py
git commit -m "feat(options): Polygon options live smoke script

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Full verification

- [ ] **Step 1: Full backend suite green**

Run: `cd backend && ../.venv/bin/python -m pytest -q`
Expected: all tests pass (includes the new config/occ/base/helpers/factory tests).

- [ ] **Step 2: App still imports**

Run: `../.venv/bin/python -c "import app.main; print('ok')"`
Expected: prints `ok` (the options package is standalone; nothing else imports it yet).

- [ ] **Step 3: Final commit (if anything outstanding)**

```bash
git add -A && git commit -m "test: full suite green after Polygon options provider

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" || echo "nothing to commit"
```
