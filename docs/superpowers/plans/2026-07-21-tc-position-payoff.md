# Triple Calendar Open-Position Payoff — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a payoff diagram (plus breakevens, max P&L, chance of profit, and net greeks) for the live Triple Calendar open position on `OptionsStrategyDetail`.

**Architecture:** A new pure module `app/options/position_payoff.py` turns an open `OptionPositionRecord` into legs and reuses the existing `app/options/combo.py` + `app/options/blackscholes.py` math. A new read-only endpoint `GET /api/strategies/{key}/options-position/payoff?ticker=…` serves it. The frontend renders it with the existing `PayoffChart.jsx`.

**Tech Stack:** Python 3.14, FastAPI, pytest (backend); React 18 + recharts (frontend). Backend tests run with `DATABASE_URL=sqlite+aiosqlite:///./_t_<name>.db` (the `tests/__init__.py` guard refuses the live DB).

## Global Constraints

- Backend tests MUST set a throwaway DB: `DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db` — run via `../.venv/bin/python -m pytest` from `backend/`.
- Reuse existing math verbatim: `payoff_at`, `payoff_curve`, `curve_stats`, `chance_of_profit` (`app/options/combo.py`), `bs_greeks` (`app/options/blackscholes.py`). Do NOT reimplement Black-Scholes.
- Leg dict shape (consumed by combo): `{"action": "BUY"|"SELL", "right": "C"|"P"|"S", "strike": float, "expiry": "YYYY-MM-DD"|date, "qty": int}`. Sign map is `LEG_SIGN = {"BUY": 1, "SELL": -1}` (in `combo.py`).
- Triple Calendar legs are ALL CALLS: each calendar `{strike, sell, buy}` = SELL near-dated call + BUY far-dated call at the same strike.
- Constants: risk-free `RATE = 0.04`, `DEFAULT_IV = 0.25` (matches the Builder).
- Dollar quantities (curve pnl, max_profit, max_loss, current_pnl, greeks) scale by `contracts`; price-space values (strikes, breakevens, spot) do NOT.
- New endpoint is READ-ONLY and UNGATED, consistent with the sibling `options-position` reads (a payoff read exposes no secrets and places no orders).
- Commit after each task. Work on branch `tc-position-payoff` (already created).

---

### Task 1: `legs_from_calendars`

**Files:**
- Create: `backend/app/options/position_payoff.py`
- Test: `backend/tests/test_position_payoff.py`

**Interfaces:**
- Produces: `legs_from_calendars(calendars: list[dict]) -> list[dict]` — each `{strike, sell, buy}` → two call legs (SELL `sell` expiry, BUY `buy` expiry), `qty=1`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_position_payoff.py
from app.options.position_payoff import legs_from_calendars


def test_legs_from_calendars_builds_sell_near_buy_far_calls():
    cals = [
        {"strike": 470.0, "sell": "2026-08-15", "buy": "2026-09-19"},
        {"strike": 480.0, "sell": "2026-08-15", "buy": "2026-09-19"},
    ]
    legs = legs_from_calendars(cals)
    assert legs == [
        {"action": "SELL", "right": "C", "strike": 470.0, "expiry": "2026-08-15", "qty": 1},
        {"action": "BUY",  "right": "C", "strike": 470.0, "expiry": "2026-09-19", "qty": 1},
        {"action": "SELL", "right": "C", "strike": 480.0, "expiry": "2026-08-15", "qty": 1},
        {"action": "BUY",  "right": "C", "strike": 480.0, "expiry": "2026-09-19", "qty": 1},
    ]


def test_legs_from_calendars_empty():
    assert legs_from_calendars([]) == []
    assert legs_from_calendars(None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.options.position_payoff'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/options/position_payoff.py
"""Turn an open Triple Calendar OptionPositionRecord into a payoff diagram + stats,
reusing the generic options math in app/options/combo.py and blackscholes.py. Pure:
no DB, no network — the router loads the record and passes it in."""
from __future__ import annotations

from datetime import date

RATE = 0.04
DEFAULT_IV = 0.25


def legs_from_calendars(calendars: list[dict] | None) -> list[dict]:
    """Each calendar {strike, sell, buy} -> a SELL near call + a BUY far call (TC is all
    calls). Leg dicts match the shape app/options/combo.py consumes."""
    legs: list[dict] = []
    for c in calendars or []:
        strike = float(c["strike"])
        legs.append({"action": "SELL", "right": "C", "strike": strike,
                     "expiry": c["sell"], "qty": 1})
        legs.append({"action": "BUY", "right": "C", "strike": strike,
                     "expiry": c["buy"], "qty": 1})
    return legs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/position_payoff.py backend/tests/test_position_payoff.py
git commit -m "feat(payoff): legs_from_calendars for TC positions"
```

---

### Task 2: `fit_iv`

**Files:**
- Modify: `backend/app/options/position_payoff.py`
- Test: `backend/tests/test_position_payoff.py`

**Interfaces:**
- Consumes: `legs_from_calendars`; `app.options.combo.payoff_at`.
- Produces: `fit_iv(legs, spot, live_value, *, today=None, default=DEFAULT_IV, r=RATE) -> tuple[float, str]` — bisection-solves the single IV that reprices the structure (both legs alive, `eval_date=today`) to `live_value`. Returns `(iv, "fit")`, or `(default, "assumed")` when spot/live_value is missing or the root isn't bracketed.

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_position_payoff.py
from datetime import date

from app.options.combo import payoff_at
from app.options.position_payoff import fit_iv, legs_from_calendars

TODAY = date(2026, 7, 21)
CALS = [
    {"strike": 470.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 480.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 490.0, "sell": "2026-08-21", "buy": "2026-09-18"},
]


def test_fit_iv_round_trips_to_live_value():
    legs = legs_from_calendars(CALS)
    spot = 480.0
    true_iv = 0.22
    live_value = payoff_at(legs, spot, TODAY, true_iv, 0.04)
    iv, source = fit_iv(legs, spot, live_value, today=TODAY)
    assert source == "fit"
    assert abs(iv - true_iv) < 1e-3


def test_fit_iv_falls_back_to_assumed_when_no_live_value():
    legs = legs_from_calendars(CALS)
    assert fit_iv(legs, 480.0, None, today=TODAY) == (0.25, "assumed")
    assert fit_iv(legs, None, 4.0, today=TODAY) == (0.25, "assumed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: FAIL — `ImportError: cannot import name 'fit_iv'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to backend/app/options/position_payoff.py (below legs_from_calendars)
def fit_iv(legs: list[dict], spot: float | None, live_value: float | None, *,
           today: date | None = None, default: float = DEFAULT_IV, r: float = RATE,
           lo: float = 0.01, hi: float = 3.0, iters: int = 60) -> tuple[float, str]:
    """Single IV that reprices the structure to its live mark today (both legs alive).
    The long far-dated leg dominates vega, so structure value is monotincreasing in IV
    and bisection converges. Fallback to `default`/"assumed" when unfittable."""
    from app.options.combo import payoff_at

    if not legs or spot is None or spot <= 0 or live_value is None:
        return default, "assumed"
    today = today or date.today()

    def f(v: float) -> float:
        return payoff_at(legs, spot, today, v, r) - live_value

    flo, fhi = f(lo), f(hi)
    if flo == 0.0:
        return lo, "fit"
    if fhi == 0.0:
        return hi, "fit"
    if (flo > 0) == (fhi > 0):
        return default, "assumed"  # live value outside the model's [lo, hi] range
    a, b = lo, hi
    for _ in range(iters):
        m = (a + b) / 2
        fm = f(m)
        if fm == 0.0:
            return round(m, 4), "fit"
        if (fm > 0) == (flo > 0):
            a, flo = m, fm
        else:
            b = m
    return round((a + b) / 2, 4), "fit"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/position_payoff.py backend/tests/test_position_payoff.py
git commit -m "feat(payoff): fit_iv anchors the model to the live mark"
```

---

### Task 3: `net_position_greeks`

**Files:**
- Modify: `backend/app/options/position_payoff.py`
- Test: `backend/tests/test_position_payoff.py`

**Interfaces:**
- Consumes: `app.options.blackscholes.bs_greeks`, `app.options.combo.LEG_SIGN`.
- Produces: `net_position_greeks(legs, spot, iv, contracts, *, today=None, r=RATE) -> dict` — sums per-leg greeks at the current spot with `sign×100×qty`, scaled by `contracts`. Keys `delta/gamma/theta/vega`, rounded to 4. Returns all-zeros when spot missing.

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_position_payoff.py
from app.options.position_payoff import net_position_greeks


def test_net_greeks_scale_linearly_with_contracts():
    legs = legs_from_calendars(CALS)
    g1 = net_position_greeks(legs, 480.0, 0.22, 1, today=TODAY)
    g2 = net_position_greeks(legs, 480.0, 0.22, 2, today=TODAY)
    assert set(g1) == {"delta", "gamma", "theta", "vega"}
    for k in g1:
        assert abs(g2[k] - 2 * g1[k]) < 1e-6
    # A long calendar is net long vega (far leg dominates).
    assert g1["vega"] > 0


def test_net_greeks_zero_without_spot():
    legs = legs_from_calendars(CALS)
    assert net_position_greeks(legs, None, 0.22, 1, today=TODAY) == {
        "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: FAIL — `ImportError: cannot import name 'net_position_greeks'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to backend/app/options/position_payoff.py
def _leg_expiry(leg: dict) -> date:
    e = leg["expiry"]
    return e if isinstance(e, date) else date.fromisoformat(e)


def net_position_greeks(legs: list[dict], spot: float | None, iv: float, contracts: int,
                        *, today: date | None = None, r: float = RATE) -> dict:
    """Position greeks at the current spot: per-leg bs_greeks, signed by BUY/SELL, ×100
    per contract-share × qty, summed and scaled by `contracts`."""
    from app.options.blackscholes import bs_greeks
    from app.options.combo import LEG_SIGN

    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if spot is None or spot <= 0:
        return net
    today = today or date.today()
    for leg in legs:
        T = max((_leg_expiry(leg) - today).days, 0) / 365.0
        g = bs_greeks(spot, leg["strike"], T, iv, r, leg["right"])
        sign = LEG_SIGN[leg["action"]]
        qty = int(leg.get("qty", 1) or 1)
        for k in net:
            net[k] += sign * 100 * qty * g[k]
    return {k: round(v * contracts, 4) for k, v in net.items()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/position_payoff.py backend/tests/test_position_payoff.py
git commit -m "feat(payoff): net_position_greeks for the open position"
```

---

### Task 4: `build_position_payoff` (orchestrator)

**Files:**
- Modify: `backend/app/options/position_payoff.py`
- Test: `backend/tests/test_position_payoff.py`

**Interfaces:**
- Consumes: `legs_from_calendars`, `fit_iv`, `net_position_greeks`; `app.options.combo.payoff_curve/curve_stats/chance_of_profit`. Reads `record.calendars`, `.net_debit`, `.last_spot`, `.last_value`, `.underlying`.
- Produces: `build_position_payoff(record, contracts, *, today=None, r=RATE, points=61) -> dict` — the full response dict (see spec "Response shape"). `{"has_position": False}` when the record has no calendars.

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_position_payoff.py
from types import SimpleNamespace

from app.options.position_payoff import build_position_payoff


def _rec(**kw):
    base = dict(calendars=CALS, net_debit=4.0, last_spot=480.0, last_value=4.6,
                underlying="QQQ")
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_payoff_full_shape_and_scaling():
    out1 = build_position_payoff(_rec(), 1, today=TODAY)
    assert out1["has_position"] is True
    assert out1["ticker"] == "QQQ"
    assert out1["eval_date"] == "2026-08-21"      # nearest sell expiry
    assert out1["iv_source"] == "fit"
    assert out1["strikes"] == [470.0, 480.0, 490.0]
    assert len(out1["curve"]) == 61
    # current mark P&L = (live_value - net_debit) * 100 * contracts
    assert out1["current_pnl"] == round((4.6 - 4.0) * 100 * 1, 2)
    assert 0.0 <= out1["chance_of_profit"] <= 1.0
    # A long calendar peaks near the middle strike (positive max profit).
    assert out1["max_profit"] > 0

    out2 = build_position_payoff(_rec(), 2, today=TODAY)
    assert out2["current_pnl"] == round((4.6 - 4.0) * 100 * 2, 2)
    assert abs(out2["max_profit"] - 2 * out1["max_profit"]) < 0.05  # dollar stats ×contracts
    assert out2["strikes"] == out1["strikes"]                        # price-space unchanged


def test_build_payoff_flat_when_no_calendars():
    assert build_position_payoff(_rec(calendars=[]), 1, today=TODAY) == {
        "has_position": False, "ticker": "QQQ"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_position_payoff'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to backend/app/options/position_payoff.py
def build_position_payoff(record, contracts: int, *, today: date | None = None,
                          r: float = RATE, points: int = 61) -> dict:
    """Assemble the payoff response for one open TC position record."""
    from app.options.combo import chance_of_profit, curve_stats, payoff_curve

    calendars = getattr(record, "calendars", None) or []
    ticker = (getattr(record, "underlying", "") or "").upper()
    if not calendars:
        return {"has_position": False, "ticker": ticker}

    today = today or date.today()
    legs = legs_from_calendars(calendars)
    strikes = sorted({float(c["strike"]) for c in calendars})
    eval_date = min(date.fromisoformat(c["sell"]) for c in calendars)
    spot = record.last_spot
    net_debit = record.net_debit or 0.0
    live_value = record.last_value

    iv, iv_source = fit_iv(legs, spot, live_value, today=today, r=r)
    lo, hi = strikes[0] * 0.9, strikes[-1] * 1.1
    raw = payoff_curve(legs, net_debit, eval_date, iv, r, lo, hi, points)
    curve = [{"price": p["price"], "pnl": round(p["pnl"] * contracts, 2)} for p in raw]
    stats = curve_stats(curve)
    T = max((eval_date - today).days, 0) / 365.0
    cop = chance_of_profit(curve, spot, iv, r, T)
    greeks = net_position_greeks(legs, spot, iv, contracts, today=today, r=r)
    current_pnl = (round((live_value - net_debit) * 100 * contracts, 2)
                   if live_value is not None else None)

    return {
        "has_position": True,
        "ticker": ticker,
        "eval_date": eval_date.isoformat(),
        "spot": round(spot, 4) if spot is not None else None,
        "iv": round(iv, 4),
        "iv_source": iv_source,
        "contracts": contracts,
        "net_debit": round(net_debit, 4),
        "live_value": round(live_value, 4) if live_value is not None else None,
        "current_pnl": current_pnl,
        "curve": curve,
        "strikes": strikes,
        "breakevens": stats["breakevens"],
        "max_profit": stats["max_profit"],
        "max_loss": stats["max_loss"],
        "chance_of_profit": cop,
        "greeks": greeks,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff.db ../.venv/bin/python -m pytest tests/test_position_payoff.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/position_payoff.py backend/tests/test_position_payoff.py
git commit -m "feat(payoff): build_position_payoff orchestrator"
```

---

### Task 5: Payoff endpoint

**Files:**
- Modify: `backend/app/routers/strategies.py` (add route after the `options_position` route, ~line 40)
- Test: `backend/tests/test_payoff_endpoint.py`

**Interfaces:**
- Consumes: `build_position_payoff`; existing module helpers `_require_ready`, `_state`, `session_scope`, `OptionPositionRecord`.
- Produces: `GET /api/strategies/{key}/options-position/payoff?ticker=<T>` → `build_position_payoff(...)` dict, or `{"has_position": False, "ticker": T}` when no open record.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_payoff_endpoint.py
"""GET /api/strategies/{key}/options-position/payoff. Run:
DATABASE_URL=sqlite+aiosqlite:///./_t_payoff_ep.db python -m pytest tests/test_payoff_endpoint.py"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord
from app.main import app

CALS = [
    {"strike": 470.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 480.0, "sell": "2026-08-21", "buy": "2026-09-18"},
    {"strike": 490.0, "sell": "2026-08-21", "buy": "2026-09-18"},
]


@pytest.mark.asyncio
async def test_payoff_endpoint_open_position():
    await init_db()
    async with session_scope() as s:
        s.add(OptionPositionRecord(
            strategy_key="strategy_2", underlying="QQQ", status="open", paper=True,
            net_debit=4.0, last_value=4.6, last_spot=480.0, calendars=CALS,
            entry_time=datetime.now(timezone.utc)))
    c = TestClient(app)
    r = c.get("/api/strategies/strategy_2/options-position/payoff?ticker=QQQ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_position"] is True
    assert body["ticker"] == "QQQ"
    assert len(body["curve"]) == 61
    assert set(body["greeks"]) == {"delta", "gamma", "theta", "vega"}


@pytest.mark.asyncio
async def test_payoff_endpoint_flat():
    await init_db()
    c = TestClient(app)
    r = c.get("/api/strategies/strategy_2/options-position/payoff?ticker=SPY")
    assert r.status_code == 200
    assert r.json() == {"has_position": False, "ticker": "SPY"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff_ep.db ../.venv/bin/python -m pytest tests/test_payoff_endpoint.py -q`
Expected: FAIL — 404 (route not registered) on the first test.

- [ ] **Step 3: Write minimal implementation**

Add this route in `backend/app/routers/strategies.py` immediately after the `options_position` function returns (right before the `async def _trades_today_count` definition):

```python
@router.get("/{key}/options-position/payoff")
async def options_position_payoff(key: str, ticker: str) -> dict:
    """Payoff diagram + stats + net greeks for one open Triple Calendar position.
    Read-only; {"has_position": false} when flat (200, not 404)."""
    from sqlalchemy import select

    from app.db.models import OptionPositionRecord
    from app.options.position_payoff import build_position_payoff

    _require_ready(key)
    tk = ticker.upper()
    st = await _state(key)
    raw = (st.tc_tickers if st else None) or []
    cmap = {str(t["ticker"]).upper(): max(1, int(t.get("contracts", 1)))
            for t in raw if t.get("ticker")}
    contracts = cmap.get(tk, max(1, int(getattr(st, "contracts", 1) or 1)) if st else 1)
    async with session_scope() as session:
        rec = (await session.execute(select(OptionPositionRecord).where(
            OptionPositionRecord.strategy_key == key,
            OptionPositionRecord.underlying == tk,
            OptionPositionRecord.status == "open"))).scalars().first()
    if rec is None:
        return {"has_position": False, "ticker": tk}
    return build_position_payoff(rec, contracts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_payoff_ep.db ../.venv/bin/python -m pytest tests/test_payoff_endpoint.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full backend suite (no regressions)**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_all.db ../.venv/bin/python -m pytest tests -q`
Expected: PASS (all prior tests + the new payoff tests). Then `rm -f _t_*.db`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/strategies.py backend/tests/test_payoff_endpoint.py
git commit -m "feat(payoff): GET options-position/payoff endpoint"
```

---

### Task 6: Frontend Payoff card

**Files:**
- Modify: `frontend/src/api.js` (add client method next to `optionsPosition`)
- Modify: `frontend/src/components/OptionsStrategyDetail.jsx` (render a Payoff card per open ticker)

**Interfaces:**
- Consumes: `GET /api/strategies/{key}/options-position/payoff?ticker=…`; existing `PayoffChart` (props `curve, breakevens, strikes, spot`).
- Produces: `api.optionsPositionPayoff(key, ticker)`.

- [ ] **Step 1: Add the API client method**

In `frontend/src/api.js`, directly after the `optionsPosition:` line, add:

```javascript
  // Payoff diagram + stats + net greeks for one open Triple Calendar position.
  optionsPositionPayoff: (key, ticker) =>
    request(`/api/strategies/${key}/options-position/payoff?ticker=${encodeURIComponent(ticker)}`),
```

- [ ] **Step 2: Import PayoffChart + fetch payoff state in OptionsStrategyDetail**

At the top of `frontend/src/components/OptionsStrategyDetail.jsx`, add the import:

```javascript
import PayoffChart from "./PayoffChart.jsx";
```

After the `const [op, setOp] = useState(null);` line, add payoff state and a loader that fetches one payoff per ticker that has a position:

```javascript
  const [payoffs, setPayoffs] = useState({}); // { [ticker]: payoffResponse }
  const loadPayoffs = (tickers) => {
    (tickers || []).forEach((t) => {
      if (!t.has_position) return;
      api.optionsPositionPayoff(keyName, t.ticker)
        .then((p) => setPayoffs((prev) => ({ ...prev, [t.ticker]: p })))
        .catch(() => {});
    });
  };
```

- [ ] **Step 3: Drive the loader from the existing op poll**

Replace the existing `loadOp` definition:

```javascript
  const loadOp = () => api.optionsPosition(keyName).then(setOp).catch(() => setOp(null));
```

with one that also refreshes payoffs:

```javascript
  const loadOp = () =>
    api.optionsPosition(keyName)
      .then((d) => { setOp(d); loadPayoffs(d?.tickers); })
      .catch(() => setOp(null));
```

- [ ] **Step 4: Render the Payoff card**

Add a helper for greeks formatting and the card block. Immediately before the final `</div>` that closes the `strategy-detail options-detail` root, insert one Payoff card per open ticker:

```jsx
      {tks.filter((t) => t.has_position).map((t) => {
        const p = payoffs[t.ticker];
        if (!p || !p.has_position) return null;
        const g = p.greeks || {};
        return (
          <section className="card" key={`payoff-${t.ticker}`}>
            <div className="card-head">
              <h2>Payoff — {t.ticker}</h2>
              <span className="pill tiny idle">
                IV {(p.iv * 100).toFixed(1)}% ({p.iv_source === "fit" ? "live-fit" : "assumed"})
              </span>
            </div>
            <PayoffChart curve={p.curve} breakevens={p.breakevens}
              strikes={p.strikes} spot={p.spot} />
            <div className="deploy-grid" style={{ marginTop: 12 }}>
              <div><div className="stat-label">Breakevens</div>
                <div className="stat-value">
                  {(p.breakevens || []).map((b) => money0(b)).join(" · ") || "—"}</div></div>
              <div><div className="stat-label">Max profit (range)</div>
                <div className="stat-value pos">{money(p.max_profit)}</div></div>
              <div><div className="stat-label">Max loss (range)</div>
                <div className="stat-value neg">{money(p.max_loss)}</div></div>
              <div><div className="stat-label">Chance of profit</div>
                <div className="stat-value">{pct(p.chance_of_profit)}</div></div>
            </div>
            <div className="deploy-grid" style={{ marginTop: 8 }}>
              <div><div className="stat-label">Delta</div>
                <div className="stat-value">{g.delta ?? "—"}</div></div>
              <div><div className="stat-label">Gamma</div>
                <div className="stat-value">{g.gamma ?? "—"}</div></div>
              <div><div className="stat-label">Theta</div>
                <div className="stat-value">{g.theta ?? "—"}</div></div>
              <div><div className="stat-label">Vega</div>
                <div className="stat-value">{g.vega ?? "—"}</div></div>
            </div>
            <p className="muted small" style={{ marginTop: 8 }}>
              Model payoff at {p.eval_date} (front-month expiry). Max profit/loss are over the
              plotted price range, not the theoretical tails.
            </p>
          </section>
        );
      })}
```

(`money`, `money0`, and `pct` already exist at the top of this file.)

- [ ] **Step 5: Build the frontend to verify it compiles**

Run: `cd frontend && npm run build`
Expected: `✓ built` with no errors (the pre-existing chunk-size warning is fine).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.js frontend/src/components/OptionsStrategyDetail.jsx
git commit -m "feat(payoff): payoff + greeks card on the live TC position page"
```

---

## Self-Review

**Spec coverage:**
- Payoff curve at near expiry → Task 4 (`eval_date = min sell expiry`), rendered Task 6. ✓
- Breakevens + max profit/loss → `curve_stats` in Task 4, shown Task 6. ✓
- Current-spot marker + live P&L → `spot` + `current_pnl` (Task 4), `PayoffChart spot` + stat (Task 6). ✓
- Chance of profit → Task 4, shown Task 6. ✓
- Net greeks panel → Task 3, shown Task 6. ✓
- Live-mark IV fit w/ assumed fallback + UI badge → Task 2 + Task 6 badge. ✓
- Per-ticker, flat → placeholder (no error) → Task 5 (`has_position:false`, 200) + Task 6 (`return null`). ✓
- Ungated read endpoint → Task 5 (no `Depends(require_auth)`). ✓
- Contracts scaling of dollar values only → Task 4 + tests. ✓
- Known-limitation labeling (max loss = plotted range) → Task 6 caption. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. ✓

**Type consistency:** `legs_from_calendars`/`fit_iv`/`net_position_greeks`/`build_position_payoff` names and signatures are identical across the module, tests, and endpoint. Response keys used in Task 6 (`curve, breakevens, strikes, spot, greeks, iv, iv_source, eval_date, max_profit, max_loss, chance_of_profit, current_pnl, has_position`) all match Task 4's returned dict. `PayoffChart` props match its definition (`curve, breakevens, strikes, spot`). ✓
