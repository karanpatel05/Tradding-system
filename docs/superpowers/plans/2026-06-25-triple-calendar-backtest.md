# Triple Calendar — Phase 1 (Position Model + Backtest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Triple Calendar strategy's core (params, IV math, leg/strike/expiry selection, breach/adjustment, entry/exit logic) and a historical backtest over real Polygon option data, with modeled IV.

**Architecture:** All new files under `app/strategies/triple_calendar/` (no shared-module edits). Pure logic (`params`, `iv`, `legs`, `strategy`) is TDD'd against a stub price source; `pricing.py` provides a real `PolygonPriceSource` over the existing `app/options` provider + Polygon stock aggregates; `backtest.py` + `scripts/run_triple_calendar.py` run it.

**Tech Stack:** Python 3.14, stdlib `math`/`datetime`, httpx (via existing options provider), pytest.

**Spec:** `docs/superpowers/specs/2026-06-25-triple-calendar-backtest-design.md`

> Run `pytest` from `backend/`. Python: `/Users/karanpatel/PycharmProjects/Tradding-system/.venv/bin/python`. Repo under git; commit messages end with the Co-Authored-By trailer.
>
> **Price-source interface (used by `strategy.py`/`backtest.py`, implemented by `pricing.py`, stubbed in tests):**
> - `underlying_close(symbol: str, day: date) -> float | None`
> - `chain_strikes(underlying: str, expiry: date) -> list[float]`
> - `option_close(underlying: str, expiry: date, strike: float, right: str, day: date) -> float | None`

---

### Task 1: Params dataclass

**Files:**
- Create: `backend/app/strategies/triple_calendar/__init__.py` (empty)
- Create: `backend/app/strategies/triple_calendar/params.py`
- Test: `backend/tests/test_tc_params.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_params.py
from app.strategies.triple_calendar.params import TripleCalendarParams


def test_defaults():
    p = TripleCalendarParams()
    assert p.underlying == "QQQ" and p.sell_dte == 21 and p.buy_dte == 28
    assert p.strike_round == 5.0 and p.em_buffer == 5.0 and p.manual_nudge == 0.0
    assert p.profit_target == 0.10 and p.time_stop_dte == 7
    assert p.vix_threshold == 13.0 and p.vix_scale == 0.5
    assert p.max_calendars == 4 and p.allow_fifth is False
    assert p.event_dates == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_params.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/strategies/triple_calendar/params.py
"""Triple Calendar config — every threshold named, none hardcoded in the logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class TripleCalendarParams:
    underlying: str = "QQQ"
    sell_dte: int = 21
    buy_dte: int = 28
    strike_round: float = 5.0
    em_buffer: float = 5.0          # added to expected move before placing outer strikes
    manual_nudge: float = 0.0       # optional $ to widen outer strikes (toward S/R)
    profit_target: float = 0.10     # close at +10% of net debit
    time_stop_dte: int = 7          # close when sell leg hits this DTE
    vix_threshold: float = 13.0     # below this, scale size down (don't skip)
    vix_scale: float = 0.5
    max_calendars: int = 4
    allow_fifth: bool = False
    risk_free_rate: float = 0.04
    div_yield: float = 0.0
    event_dates: list[date] = field(default_factory=list)
```
Also create empty `backend/app/strategies/triple_calendar/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_tc_params.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/__init__.py backend/app/strategies/triple_calendar/params.py backend/tests/test_tc_params.py
git commit -m "feat(triple_calendar): params dataclass

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Black-Scholes price + implied vol

**Files:**
- Create: `backend/app/strategies/triple_calendar/iv.py`
- Test: `backend/tests/test_tc_iv.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_iv.py
from app.strategies.triple_calendar.iv import bs_price, implied_vol


def test_bs_price_atm_call_put_positive():
    c = bs_price("C", 100, 100, 0.25, 0.04, 0.20)
    p = bs_price("P", 100, 100, 0.25, 0.04, 0.20)
    assert c > 0 and p > 0


def test_implied_vol_round_trip():
    price = bs_price("C", 100, 100, 0.25, 0.04, 0.20)
    iv = implied_vol(price, "C", 100, 100, 0.25, 0.04)
    assert iv is not None and abs(iv - 0.20) < 1e-3


def test_implied_vol_none_on_nonpositive():
    assert implied_vol(0.0, "C", 100, 100, 0.25, 0.04) is None
    assert implied_vol(5.0, "C", 100, 100, 0.0, 0.04) is None  # no time
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_iv.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/strategies/triple_calendar/iv.py
"""European Black-Scholes price + implied-vol inversion (bisection). Used to model
IV from Polygon's option closes for the regime breakdown."""
from __future__ import annotations

from math import erf, exp, log, sqrt


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_price(kind: str, S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if kind == "C" else max(K - S, 0.0)
    d1 = (log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if kind == "C":
        return S * exp(-q * T) * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
    return K * exp(-r * T) * _norm_cdf(-d2) - S * exp(-q * T) * _norm_cdf(-d1)


def implied_vol(price: float, kind: str, S: float, K: float, T: float, r: float,
                q: float = 0.0) -> float | None:
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(kind, S, K, T, r, hi, q) < price:   # price above what 500% vol can produce
        return None
    for _ in range(100):
        mid = (lo + hi) / 2.0
        diff = bs_price(kind, S, K, T, r, mid, q) - price
        if abs(diff) < 1e-6:
            return mid
        if diff < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_tc_iv.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/iv.py backend/tests/test_tc_iv.py
git commit -m "feat(triple_calendar): Black-Scholes price + implied-vol inversion

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Position model + strike/expiry selection

**Files:**
- Create: `backend/app/strategies/triple_calendar/legs.py`
- Test: `backend/tests/test_tc_legs.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_legs.py
from datetime import date

from app.strategies.triple_calendar.legs import (Calendar, atm_strike, expected_move,
                                                 nearest_friday, outer_strikes, snap, target_expiry)


def test_nearest_friday():
    assert nearest_friday(date(2026, 6, 24)) == date(2026, 6, 26)   # Wed -> Fri (+2)
    assert nearest_friday(date(2026, 6, 26)) == date(2026, 6, 26)   # Fri -> same


def test_target_expiry_lands_on_friday():
    exp = target_expiry(date(2026, 6, 25), 21)
    assert exp.weekday() == 4 and abs((exp - date(2026, 6, 25)).days - 21) <= 3


def test_atm_and_outer_strikes():
    assert atm_strike(443.2, 5.0) == 445.0
    assert expected_move(6.0, 5.5) == 11.5
    lo, up = outer_strikes(445.0, 11.5, 5.0, 5.0, nudge=0.0)
    assert up == 460.0 and lo == 430.0          # 445 ± round(16.5/5)*5 = ±15
    lo2, up2 = outer_strikes(445.0, 11.5, 5.0, 5.0, nudge=5.0)
    assert up2 == 465.0 and lo2 == 425.0        # widened by the nudge


def test_snap_to_available_strikes():
    assert snap(447.0, [440.0, 445.0, 450.0]) == 445.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_legs.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/strategies/triple_calendar/legs.py
"""Triple Calendar position model + strike/expiry selection + breach/adjustment.
Pure (no I/O); shared by backtest now and live (Phase 2) later."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class Calendar:
    strike: float
    sell_expiry: date    # near-dated short leg
    buy_expiry: date     # longer-dated long leg


@dataclass
class Position:
    underlying: str
    entry_date: date
    calendars: list[Calendar] = field(default_factory=list)
    net_debit: float = 0.0
    size: float = 1.0


def nearest_friday(d: date) -> date:
    days = 4 - d.weekday()       # Friday == weekday 4
    if days > 3:
        days -= 7
    if days < -3:
        days += 7
    return d + timedelta(days=days)


def target_expiry(entry: date, dte: int) -> date:
    return nearest_friday(entry + timedelta(days=dte))


def atm_strike(price: float, round_to: float) -> float:
    return round(price / round_to) * round_to


def expected_move(call_mid: float, put_mid: float) -> float:
    return call_mid + put_mid


def outer_strikes(atm: float, em: float, buffer: float, round_to: float,
                  nudge: float = 0.0) -> tuple[float, float]:
    width = em + buffer + nudge
    lower = round((atm - width) / round_to) * round_to
    upper = round((atm + width) / round_to) * round_to
    return lower, upper


def snap(strike: float, available: list[float]) -> float:
    return min(available, key=lambda s: abs(s - strike)) if available else strike
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_tc_legs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/legs.py backend/tests/test_tc_legs.py
git commit -m "feat(triple_calendar): position model + strike/expiry selection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Breach detection + calendar adjustment

**Files:**
- Modify: `backend/app/strategies/triple_calendar/legs.py`
- Test: `backend/tests/test_tc_adjust.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_adjust.py
from datetime import date

from app.strategies.triple_calendar.legs import Calendar, Position, add_calendar, breached
from app.strategies.triple_calendar.params import TripleCalendarParams

SE, BE = date(2026, 7, 17), date(2026, 7, 24)


def _pos():
    return Position(underlying="QQQ", entry_date=date(2026, 6, 25), calendars=[
        Calendar(430.0, SE, BE), Calendar(445.0, SE, BE), Calendar(460.0, SE, BE)])


def test_breached_sides():
    assert breached(_pos(), 470.0) == "upper"
    assert breached(_pos(), 420.0) == "lower"
    assert breached(_pos(), 445.0) is None


def test_add_calendar_extends_and_caps():
    p = TripleCalendarParams()  # max 4, allow_fifth False
    pos = _pos()
    c = add_calendar(pos, "upper", SE, BE, p)
    assert c is not None and c.strike == 475.0 and len(pos.calendars) == 4  # 460 + spacing 15
    assert add_calendar(pos, "upper", SE, BE, p) is None                    # capped at 4


def test_fifth_allowed_when_flagged():
    p = TripleCalendarParams(allow_fifth=True)
    pos = _pos()
    add_calendar(pos, "upper", SE, BE, p)        # 4th
    c = add_calendar(pos, "lower", SE, BE, p)    # 5th allowed
    assert c is not None and len(pos.calendars) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_adjust.py -v`
Expected: FAIL (`breached`/`add_calendar` missing).

- [ ] **Step 3: Implement (append to `legs.py`)**

```python
def breached(position: Position, price: float) -> str | None:
    strikes = [c.strike for c in position.calendars]
    if price > max(strikes):
        return "upper"
    if price < min(strikes):
        return "lower"
    return None


def _spacing(strikes: list[float], default: float) -> float:
    s = sorted(strikes)
    return (s[-1] - s[0]) / (len(s) - 1) if len(s) > 1 else default


def add_calendar(position: Position, side: str, sell_expiry: date, buy_expiry: date,
                 params) -> Calendar | None:
    """Add one calendar further out on the breached side. Never removes existing ones.
    Returns the new Calendar, or None if the max-calendar cap is reached."""
    cap = params.max_calendars + (1 if params.allow_fifth else 0)
    if len(position.calendars) >= cap:
        return None
    strikes = [c.strike for c in position.calendars]
    spacing = _spacing(strikes, params.strike_round)
    raw = (max(strikes) + spacing) if side == "upper" else (min(strikes) - spacing)
    strike = round(raw / params.strike_round) * params.strike_round
    cal = Calendar(strike=strike, sell_expiry=sell_expiry, buy_expiry=buy_expiry)
    position.calendars.append(cal)
    return cal
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_tc_adjust.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/legs.py backend/tests/test_tc_adjust.py
git commit -m "feat(triple_calendar): breach detection + calendar adjustment

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Core entry/exit/management logic

**Files:**
- Create: `backend/app/strategies/triple_calendar/strategy.py`
- Test: `backend/tests/test_tc_strategy.py`

Calendar value at a given day = `long(buy) - short(sell)` close, summed over calendars. Net
debit is that same quantity at entry; P&L = current value − net debit.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_strategy.py
from datetime import date

from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.strategy import calendar_debit, decide_entry, manage


class StubSource:
    """Deterministic source: option price rises with time-to-expiry, so the longer-dated
    buy leg is always worth more than the short sell leg -> a positive calendar debit,
    without needing to know the chosen expiries in advance."""
    def __init__(self, spot, strikes):
        self.spot, self.strikes = spot, strikes

    def underlying_close(self, symbol, day):
        return self.spot

    def chain_strikes(self, underlying, expiry):
        return self.strikes

    def option_close(self, underlying, expiry, strike, right, day):
        return 1.0 + 0.1 * max((expiry - day).days, 0)


def test_decide_entry_builds_three_calendars():
    src = StubSource(445.0, [float(r) for r in range(400, 491, 5)])
    p = TripleCalendarParams(underlying="QQQ")
    pos = decide_entry(date(2026, 6, 25), src, p, events=[], vix=None)
    assert pos is not None and len(pos.calendars) == 3
    assert pos.net_debit > 0


def test_event_filter_blocks_entry():
    src = StubSource(445.0, [float(r) for r in range(400, 491, 5)])
    p = TripleCalendarParams()
    from app.strategies.triple_calendar.legs import target_expiry
    ev = target_expiry(date(2026, 6, 25), 21)  # event on/before the sell expiry
    assert decide_entry(date(2026, 6, 25), src, p, events=[ev], vix=None) is None


def test_manage_exits_on_profit_target():
    p = TripleCalendarParams(profit_target=0.10)
    from app.strategies.triple_calendar.legs import Calendar, Position
    se, be = date(2026, 7, 17), date(2026, 7, 24)
    pos = Position("QQQ", date(2026, 6, 25), [Calendar(445.0, se, be)], net_debit=1.0)

    class S:
        def underlying_close(self, *a): return 445.0
        def chain_strikes(self, *a): return [445.0]
        def option_close(self, u, e, k, r, d): return 3.0 if e == be else 1.9  # value 1.1 = +10%
    action, _ = manage(pos, date(2026, 7, 1), S(), p)
    assert action == "exit"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_strategy.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/strategies/triple_calendar/strategy.py
"""Triple Calendar core: entry construction + exit/adjustment management. Pure given
a price source (see the price-source interface in the plan header). Shared by the
backtest and, in Phase 2, the live worker."""
from __future__ import annotations

from datetime import date, timedelta

from app.strategies.triple_calendar.legs import (Calendar, Position, add_calendar,
                                                 atm_strike, breached, expected_move,
                                                 outer_strikes, snap, target_expiry)


def calendar_debit(source, underlying, cal: Calendar, day: date) -> float | None:
    """Net debit/value of one calendar = long(buy) − short(sell), using calls."""
    sell = source.option_close(underlying, cal.sell_expiry, cal.strike, "C", day)
    buy = source.option_close(underlying, cal.buy_expiry, cal.strike, "C", day)
    if sell is None or buy is None:
        return None
    return buy - sell


def position_value(source, position: Position, day: date) -> float | None:
    total = 0.0
    for cal in position.calendars:
        v = calendar_debit(source, position.underlying, cal, day)
        if v is None:
            return None
        total += v
    return total


def decide_entry(day: date, source, params, events: list[date], vix: float | None) -> Position | None:
    u = params.underlying
    spot = source.underlying_close(u, day)
    if spot is None:
        return None
    sell_expiry = target_expiry(day, params.sell_dte)
    buy_expiry = target_expiry(sell_expiry + timedelta(days=7), 0)  # next Friday after sell
    # Event filter: block if a macro event falls while the (sell) leg is open.
    if any(day < e <= sell_expiry for e in events):
        return None
    strikes = source.chain_strikes(u, sell_expiry)
    atm = snap(atm_strike(spot, params.strike_round), strikes)
    call = source.option_close(u, sell_expiry, atm, "C", day)
    put = source.option_close(u, sell_expiry, atm, "P", day)
    if call is None or put is None:
        return None
    em = expected_move(call, put)
    lo, up = outer_strikes(atm, em, params.em_buffer, params.strike_round, params.manual_nudge)
    chosen = [snap(s, strikes) for s in (lo, atm, up)]
    cals = [Calendar(s, sell_expiry, buy_expiry) for s in sorted(set(chosen))]
    pos = Position(underlying=u, entry_date=day, calendars=cals)
    debit = position_value(source, pos, day)
    if debit is None or debit <= 0:
        return None
    pos.net_debit = debit
    pos.size = params.vix_scale if (vix is not None and vix < params.vix_threshold) else 1.0
    return pos


def manage(position: Position, day: date, source, params) -> tuple[str, dict]:
    """Return ('exit'|'adjust'|'hold', info). Order: profit target, time stop, breach."""
    value = position_value(source, position, day)
    if value is not None and position.net_debit > 0:
        pnl_pct = (value - position.net_debit) / position.net_debit
        if pnl_pct >= params.profit_target:
            return "exit", {"reason": "target", "value": value, "pnl_pct": pnl_pct}
    sell_expiry = position.calendars[0].sell_expiry
    if (sell_expiry - day).days <= params.time_stop_dte:
        return "exit", {"reason": "time_stop", "value": value}
    spot = source.underlying_close(position.underlying, day)
    if spot is not None:
        side = breached(position, spot)
        if side:
            cal = add_calendar(position, side, sell_expiry, position.calendars[0].buy_expiry, params)
            if cal is not None:
                add = calendar_debit(source, position.underlying, cal, day) or 0.0
                position.net_debit += add
                return "adjust", {"side": side, "strike": cal.strike}
    return "hold", {"value": value}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_tc_strategy.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/strategy.py backend/tests/test_tc_strategy.py
git commit -m "feat(triple_calendar): core entry + exit/adjustment management

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Backtest loop + metrics

**Files:**
- Create: `backend/app/strategies/triple_calendar/backtest.py`
- Test: `backend/tests/test_tc_backtest.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tc_backtest.py
from datetime import date, timedelta

from app.strategies.triple_calendar.backtest import compute_metrics, run_backtest
from app.strategies.triple_calendar.params import TripleCalendarParams


class RisingSource:
    """ATM calendar that gains value over time -> hits the 10% target."""
    def __init__(self): self.start = date(2026, 6, 1)
    def trading_days(self, s, e):
        d, out = s, []
        while d <= e:
            if d.weekday() < 5: out.append(d)
            d += timedelta(days=1)
        return out
    def underlying_close(self, sym, day): return 445.0
    def chain_strikes(self, u, e): return [r for r in range(400, 491, 5)]
    def option_close(self, u, expiry, strike, right, day):
        base = 3.0 if expiry == max_expiry(self) else 2.0
        # long leg appreciates ~ each day after entry -> debit grows
        return base + (0.02 * (day - self.start).days if expiry == max_expiry(self) else 0.0)


def max_expiry(_src):  # the buy (later) expiry chosen for entries in this window
    from app.strategies.triple_calendar.legs import target_expiry
    se = target_expiry(date(2026, 6, 2), 21)
    return target_expiry(se + timedelta(days=7), 0)


def test_backtest_runs_and_records_a_trade():
    p = TripleCalendarParams()
    trades = run_backtest(RisingSource(), p, date(2026, 6, 2), date(2026, 7, 20))
    assert len(trades) >= 1
    t = trades[0]
    assert {"entry_date", "exit_date", "pnl_pct", "hold_days", "reason"} <= set(t)


def test_compute_metrics_shape():
    trades = [{"pnl_pct": 0.1, "hold_days": 5, "reason": "target", "entry_iv": 0.2},
              {"pnl_pct": -0.05, "hold_days": 9, "reason": "time_stop", "entry_iv": 0.4}]
    m = compute_metrics(trades)
    assert m["total_trades"] == 2 and m["win_rate"] == 0.5
    assert "avg_hold_days" in m and "max_drawdown" in m and "iv_regime" in m
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_tc_backtest.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/app/strategies/triple_calendar/backtest.py
"""Triple Calendar backtest: drive the shared core over a date range from a price
source, record per-trade results, and summarize (incl. modeled-IV regime buckets)."""
from __future__ import annotations

from datetime import date

from app.strategies.triple_calendar.iv import implied_vol
from app.strategies.triple_calendar.strategy import decide_entry, manage, position_value


def _entry_iv(source, params, pos) -> float | None:
    """Modeled IV of the ATM short call at entry (middle calendar)."""
    cal = pos.calendars[len(pos.calendars) // 2]
    px = source.option_close(params.underlying, cal.sell_expiry, cal.strike, "C", pos.entry_date)
    spot = source.underlying_close(params.underlying, pos.entry_date)
    if px is None or spot is None:
        return None
    T = max((cal.sell_expiry - pos.entry_date).days, 1) / 365.0
    return implied_vol(px, "C", spot, cal.strike, T, params.risk_free_rate, params.div_yield)


def run_backtest(source, params, start: date, end: date, vix=None) -> list[dict]:
    days = source.trading_days(start, end)
    trades: list[dict] = []
    pos = None
    entry_iv = None
    for day in days:
        if pos is None:
            pos = decide_entry(day, source, params, params.event_dates, vix)
            if pos is not None:
                entry_iv = _entry_iv(source, params, pos)
            continue
        action, info = manage(pos, day, source, params)
        if action == "exit":
            value = info.get("value") or position_value(source, pos, day) or pos.net_debit
            trades.append({
                "underlying": params.underlying, "entry_date": pos.entry_date.isoformat(),
                "exit_date": day.isoformat(), "reason": info["reason"],
                "net_debit": round(pos.net_debit, 4),
                "pnl_pct": round((value - pos.net_debit) / pos.net_debit, 4) if pos.net_debit else 0.0,
                "hold_days": (day - pos.entry_date).days,
                "calendars": len(pos.calendars), "entry_iv": entry_iv,
            })
            pos, entry_iv = None, None
    return trades


def _max_drawdown(pnls: list[float]) -> float:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in pnls:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return round(mdd, 4)


def _bucket(iv: float | None) -> str:
    if iv is None:
        return "unknown"
    return "low(<20%)" if iv < 0.20 else "mid(20-35%)" if iv < 0.35 else "high(>=35%)"


def compute_metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"total_trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
                "avg_hold_days": 0.0, "max_drawdown": 0.0, "iv_regime": {}}
    pnls = [t["pnl_pct"] for t in trades]
    wins = sum(1 for r in pnls if r > 0)
    regime: dict[str, list[float]] = {}
    for t in trades:
        regime.setdefault(_bucket(t.get("entry_iv")), []).append(t["pnl_pct"])
    iv_regime = {k: {"trades": len(v), "win_rate": round(sum(1 for r in v if r > 0) / len(v), 4),
                     "avg_pnl_pct": round(sum(v) / len(v), 4)} for k, v in regime.items()}
    return {
        "total_trades": n,
        "win_rate": round(wins / n, 4),
        "avg_pnl_pct": round(sum(pnls) / n, 4),
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / n, 1),
        "max_drawdown": _max_drawdown(pnls),
        "iv_regime": iv_regime,
    }
```
(Note: the price source for the backtest also exposes `trading_days(start, end) -> list[date]`;
the `PolygonPriceSource` in Task 7 implements it from the underlying's daily bars.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_tc_backtest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategies/triple_calendar/backtest.py backend/tests/test_tc_backtest.py
git commit -m "feat(triple_calendar): backtest loop + metrics (incl. IV regime)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Polygon price source + runner

**Files:**
- Create: `backend/app/strategies/triple_calendar/pricing.py`
- Create: `backend/scripts/run_triple_calendar.py`

- [ ] **Step 1: Implement `pricing.py`**

```python
# backend/app/strategies/triple_calendar/pricing.py
"""Real price source for the Triple Calendar backtest, over Polygon. Option closes
come from the existing options provider's daily aggregates; the underlying's daily
closes come from Polygon stock aggregates (same key). Per-(contract) daily series are
cached in-memory per run to stay pacing-friendly."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from app.config import get_settings
from app.options import get_options_provider
from app.options.contracts import occ_ticker


class PolygonPriceSource:
    def __init__(self, start: date, end: date):
        self._prov = get_options_provider()
        self._settings = get_settings()
        self._start, self._end = start, end
        self._under: dict[str, dict[date, float]] = {}    # symbol -> {day: close}
        self._opt: dict[str, dict[date, float]] = {}      # occ ticker -> {day: close}
        self._chain: dict[tuple, list[float]] = {}

    # ----- underlying -----
    def _load_underlying(self, symbol: str) -> dict[date, float]:
        if symbol not in self._under:
            import httpx
            url = (f"{self._settings.polygon_base_url}/v2/aggs/ticker/{symbol}/range/1/day/"
                   f"{self._start.isoformat()}/{self._end.isoformat()}")
            r = httpx.get(url, params={"adjusted": "true", "sort": "asc", "limit": 50000,
                                       "apiKey": self._settings.polygon_api_key}, timeout=30)
            r.raise_for_status()
            out = {}
            for b in r.json().get("results", []):
                d = datetime.utcfromtimestamp(b["t"] / 1000).date()
                out[d] = float(b["c"])
            self._under[symbol] = out
        return self._under[symbol]

    def trading_days(self, start: date, end: date) -> list[date]:
        # placeholder symbol resolved by callers via underlying_close; use the param underlying
        days = sorted(d for d in self._load_underlying(self._settings_underlying) if start <= d <= end)
        return days

    def underlying_close(self, symbol: str, day: date) -> float | None:
        self._settings_underlying = symbol
        return self._load_underlying(symbol).get(day)

    # ----- options -----
    def _load_option(self, ticker: str) -> dict[date, float]:
        if ticker not in self._opt:
            bars = asyncio.run(self._prov.get_aggregates(ticker, "1d", self._start, self._end))
            self._opt[ticker] = {
                datetime.utcfromtimestamp(b["time"] / 1000).date(): b["close"] for b in bars}
        return self._opt[ticker]

    def option_close(self, underlying: str, expiry: date, strike: float, right: str,
                     day: date) -> float | None:
        ticker = occ_ticker(underlying, expiry, right, strike)
        return self._load_option(ticker).get(day)

    def chain_strikes(self, underlying: str, expiry: date) -> list[float]:
        key = (underlying, expiry)
        if key not in self._chain:
            cons = asyncio.run(self._prov.list_contracts(
                underlying, expiration_date=expiry.isoformat(), contract_type="call", limit=1000))
            self._chain[key] = sorted({c.strike for c in cons})
        return self._chain[key]
```
> Note: `trading_days` references `self._settings_underlying`, set on first `underlying_close`.
> The runner calls `underlying_close(params.underlying, start)` once before `run_backtest`
> so the attribute exists; this is documented in the runner below.

- [ ] **Step 2: Implement `scripts/run_triple_calendar.py`**

```python
# backend/scripts/run_triple_calendar.py
"""Run the Triple Calendar backtest on Polygon option data. Standalone, report only.

    python -m scripts.run_triple_calendar               # QQQ, ~last 2y
    python -m scripts.run_triple_calendar SPY 2024-07-01 2026-06-01
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from app.strategies.triple_calendar.backtest import compute_metrics, run_backtest
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.pricing import PolygonPriceSource


def main() -> int:
    underlying = sys.argv[1].upper() if len(sys.argv) > 1 else "QQQ"
    start = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() - timedelta(days=730)
    end = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.today()
    params = TripleCalendarParams(underlying=underlying)
    source = PolygonPriceSource(start, end)
    source.underlying_close(underlying, start)   # prime the underlying series

    print(f"Triple Calendar backtest | {underlying} | {start} -> {end} "
          f"| sell {params.sell_dte}DTE / buy {params.buy_dte}DTE | target {params.profit_target:.0%}")
    trades = run_backtest(source, params, start, end)
    m = compute_metrics(trades)
    print(f"\nTrades {m['total_trades']} | win {m['win_rate']:.0%} | avg P&L "
          f"{m['avg_pnl_pct']:.1%} | avg hold {m['avg_hold_days']}d | maxDD {m['max_drawdown']:.1%}")
    print("IV regime:")
    for bucket, r in sorted(m["iv_regime"].items()):
        print(f"  {bucket:<14} trades {r['trades']:>3}  win {r['win_rate']:.0%}  avg {r['avg_pnl_pct']:+.1%}")
    if not trades:
        print("\n(No completed trades — option history may be too thin for the chosen "
              "expiries/window on the current Polygon plan.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run the live backtest**

Run: `cd backend && ../.venv/bin/python -m scripts.run_triple_calendar QQQ 2024-07-01 2026-06-01`
Expected: prints metrics + IV-regime breakdown, OR a clean "option history too thin" message
(both acceptable — they prove the pipeline runs end-to-end on real data). If it errors on a
`PolygonNotAuthorized`, the requested window predates the plan's lookback; shorten it.

- [ ] **Step 4: Commit**

```bash
git add backend/app/strategies/triple_calendar/pricing.py backend/scripts/run_triple_calendar.py
git commit -m "feat(triple_calendar): Polygon price source + backtest runner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Full verification

- [ ] **Step 1: Full backend suite**

Run: `cd backend && ../.venv/bin/python -m pytest -q`
Expected: all tests pass (incl. the new tc_* tests).

- [ ] **Step 2: App still imports (no shared module broken)**

Run: `../.venv/bin/python -c "import app.main; print('ok')"`
Expected: prints `ok` (Phase 1 added only new files).

- [ ] **Step 3: Final commit (if anything outstanding)**

```bash
git add -A && git commit -m "test: triple calendar phase 1 green" || echo "nothing to commit"
```
