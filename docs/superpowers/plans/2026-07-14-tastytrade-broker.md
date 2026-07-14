# Tastytrade Sole Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace IBKR entirely: Tastytrade becomes the sole broker (orders, accounts/positions, reconciliation, kill switch, status) and the sole data source; all IBKR code is deleted.

**Architecture:** Strangler swap. A new `app/broker/` package implements the exact public API of `app/engine/execution.py` and `app/ibkr/account.py` against the Tastytrade SDK; `execution.py` becomes a thin re-export; then gateway/auth/status/reconciliation swap sources; finally the IBKR tree is deleted with grep-gates. The system stays green at every commit.

**Tech Stack:** Python 3.14, FastAPI, `tastytrade==12.4.1` (OAuth Session already configured), existing `app/tastytrade/{session,streamer}` + `app/marketdata/facade`. `ib_async` removed at the end.

**Spec:** `docs/superpowers/specs/2026-07-13-tastytrade-broker-design.md`

## Global Constraints

- `enable_live_orders` defaults **False**. With it False, NO code path may construct a network order call — tests use fake accounts whose `place_order` RAISES if touched.
- Live placements call `place_order(..., dry_run=True)` first; a dry-run error rejects locally (journaled) and nothing is placed. (SDK default for `dry_run` is True — every real placement must pass `dry_run=False` explicitly.)
- Simulated (paper) fills price at the sanity-guarded facade mid ONLY; a rejected/missing mid → no fill this attempt (`transmitted: False`, detail says why), never a made-up price.
- SAFE_MODE blocks all order submission (existing `is_safe_mode()` checks stay at the top of every submit path).
- Builder never transmits: no `app.broker` import under `app/options/` or `routers/builder.py` (grep-gated in Task 14).
- Public call signatures preserved verbatim: `submit_equity_order(symbol, intent, quantity, *, reason)`, `submit_bracket_order(symbol, side, quantity, entry_price, stop, target, *, reason)`, `cancel_symbol_orders(symbol)`, `poll_bracket_fill(symbol, ref=None)`, `flatten_all()`, `submit_combo_order(underlying, legs, *, net_price, action, reason, quantity)` — return-dict shapes unchanged (`transmitted`, `paper`, `detail`, …). Callers (worker, TC, routers) must not change in Tasks 1–6.
- Position shape preserved: `NormalizedPosition(symbol, quantity, quantity_direction, instrument_type, avg_price, account)`; account-summary dict keys preserved (`net_liquidation`, `total_cash`, `buying_power`, `available_funds`, `gross_position_value`, `unrealized_pnl`, `realized_pnl`, `init_margin`, `maint_margin`, `account`, `currency`).
- **SDK price sign convention:** `NewOrder.price` is a `Decimal`; NEGATIVE = debit (you pay), POSITIVE = credit (you receive). Implementers must confirm against the installed SDK's `tastytrade/order.py` docstrings in Task 2 and note it in the report.
- Backend tests: `cd backend && rm -f _t_X.db && DATABASE_URL=sqlite+aiosqlite:///./_t_X.db ../.venv/bin/python -m pytest <files> -q; rm -f _t_X.db`. Venv python: `../.venv/bin/python`.
- Full pytest suite must be green at the end of every task (`pytest tests/ -q`).

## File Structure

```
backend/app/broker/__init__.py       (new, empty)
backend/app/broker/account.py       (new) Account cache, balances, positions, live orders
backend/app/broker/orders.py        (new) equity/bracket/combo submit + gate + flatten
backend/app/broker/status.py        (new) probe-backed readiness (replaces ConnState)
backend/app/engine/execution.py     (Task 6: thin re-export of app.broker.orders)
backend/app/engine/reconciliation.py (Task 7: broker-sourced snapshot)
backend/app/engine/recovery.py      (Task 7: gateway-free recovery)
backend/app/auth/{manager,gate,routes}.py (Task 8: TT-backed auth)
backend/app/routers/{dashboard,ws}.py     (Task 9: broker account/positions)
backend/app/services/market_stream.py     (Task 9: DXLink cache-backed)
backend/app/options/monitor.py      (Task 10: probe-gated)
backend/app/strategies/triple_calendar/strategy_class.py (Task 10: probe-gated)
backend/app/engine/worker.py        (Task 10: pause condition swap only)
backend/app/marketdata/facade.py    (Task 11: IBKR fallbacks removed)
backend/app/providers/*             (Task 11: TT-only factory)
DELETED in Task 12: app/ibkr/*, engine/ibkr_orders.py, providers/ibkr_historical.py
frontend/src/components/{ApiTab,DataBrokerChips,ConnectionBanner}.jsx (Task 13)
```

---

### Task 1: `app/broker/account.py` — account, balances, positions

**Files:**
- Create: `backend/app/broker/__init__.py` (empty), `backend/app/broker/account.py`
- Test: `backend/tests/test_broker_account.py`

**Interfaces:**
- Produces: `async get_tt_account(refresh=False) -> Account | None` (cached; None when no session/creds); `def reset_tt_account() -> None`; `async balances() -> dict` (the 11-key normalized summary shape, `{}` on failure); `async positions() -> list[NormalizedPosition]` (nonzero only); `async live_orders() -> list` ; `async cancel_order(order_id: int) -> bool`. `NormalizedPosition` dataclass moves here (same fields as `app/ibkr/account.py`'s).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_account.py
"""Broker account reads: cached Account threading, normalized balances/positions,
never-raise degradation. All SDK objects faked — no network."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import account as acct


class _FakeAccount:
    account_number = "5WT00000"

    async def get_balances(self, session, currency="USD"):
        return SimpleNamespace(
            net_liquidating_value=Decimal("25000.50"), cash_balance=Decimal("10000"),
            equity_buying_power=Decimal("50000"), derivative_buying_power=Decimal("20000"),
            margin_equity=Decimal("25000.50"))

    async def get_positions(self, session, include_marks=None):
        return [SimpleNamespace(symbol="AAPL", quantity=Decimal("10"),
                                quantity_direction="Long", instrument_type="Equity",
                                average_open_price=Decimal("150.25"),
                                account_number="5WT00000", multiplier=1),
                SimpleNamespace(symbol="QQQ   260807C00725000", quantity=Decimal("0"),
                                quantity_direction="Zero", instrument_type="Equity Option",
                                average_open_price=Decimal("1"),
                                account_number="5WT00000", multiplier=100)]


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    async def fake_session():
        return object()
    monkeypatch.setattr(acct, "_get_session", fake_session)
    async def fake_fetch(session):
        return _FakeAccount()
    monkeypatch.setattr(acct, "_fetch_account", fake_fetch)
    acct.reset_tt_account()
    yield
    acct.reset_tt_account()


@pytest.mark.asyncio
async def test_account_cached_and_reset():
    a1 = await acct.get_tt_account()
    a2 = await acct.get_tt_account()
    assert a1 is a2 and a1.account_number == "5WT00000"
    acct.reset_tt_account()
    assert (await acct.get_tt_account()) is not a1


@pytest.mark.asyncio
async def test_balances_normalized_shape():
    b = await acct.balances()
    assert b["net_liquidation"] == 25000.50
    assert b["total_cash"] == 10000.0
    assert b["buying_power"] == 50000.0
    assert b["account"] == "5WT00000" and b["currency"] == "USD"
    for k in ("available_funds", "gross_position_value", "unrealized_pnl",
              "realized_pnl", "init_margin", "maint_margin"):
        assert k in b   # keys exist (None where TT has no equivalent)


@pytest.mark.asyncio
async def test_positions_normalized_nonzero_only():
    ps = await acct.positions()
    assert len(ps) == 1
    p = ps[0]
    assert (p.symbol, p.quantity, p.quantity_direction, p.instrument_type,
            p.avg_price, p.account) == ("AAPL", 10, "Long", "Equity", 150.25, "5WT00000")


@pytest.mark.asyncio
async def test_no_session_degrades(monkeypatch):
    async def none_session():
        return None
    monkeypatch.setattr(acct, "_get_session", none_session)
    acct.reset_tt_account()
    assert await acct.get_tt_account() is None
    assert await acct.balances() == {}
    assert await acct.positions() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_ba.db && DATABASE_URL=sqlite+aiosqlite:///./_t_ba.db ../.venv/bin/python -m pytest tests/test_broker_account.py -q; rm -f _t_ba.db`
Expected: FAIL — `app.broker` missing.

- [ ] **Step 3: Implement**

```python
# backend/app/broker/account.py
"""Tastytrade account access, normalized to the shapes the dashboard/kill-switch already
consume. The real Account object is threaded everywhere (SDK gotcha: it has many required
fields and cannot be rebuilt from account_number). Reads never raise — they degrade to
None/{}/[] and the caller/UI handles absence."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("trading.broker.account")

_account = None
_lock = asyncio.Lock()


@dataclass
class NormalizedPosition:
    symbol: str
    quantity: int
    quantity_direction: str   # "Long" | "Short" | "Flat"
    instrument_type: str      # "Equity" | "Equity Option" | raw TT type
    avg_price: float
    account: str


async def _get_session():
    from app.tastytrade.session import get_tt_session
    return await get_tt_session()


async def _fetch_account(session):
    from tastytrade import Account

    from app.config import get_settings
    number = get_settings().tt_account_number or None
    got = await Account.get(session, number) if number else await Account.get(session)
    if isinstance(got, list):
        return got[0] if got else None
    return got


async def get_tt_account(refresh: bool = False):
    global _account
    if _account is not None and not refresh:
        return _account
    session = await _get_session()
    if session is None:
        return None
    async with _lock:
        if _account is None or refresh:
            try:
                _account = await _fetch_account(session)
            except Exception:  # noqa: BLE001 — degrade; probe/UI surfaces broker health
                logger.warning("Account fetch failed", exc_info=True)
                _account = None
    return _account


def reset_tt_account() -> None:
    global _account
    _account = None


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def balances() -> dict:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return {}
    try:
        b = await a.get_balances(session)
    except Exception:  # noqa: BLE001
        logger.warning("Balance fetch failed", exc_info=True)
        return {}
    return {
        "net_liquidation": _f(getattr(b, "net_liquidating_value", None)),
        "total_cash": _f(getattr(b, "cash_balance", None)),
        "buying_power": _f(getattr(b, "equity_buying_power", None)),
        "available_funds": _f(getattr(b, "derivative_buying_power", None)),
        "gross_position_value": None,
        "unrealized_pnl": None,          # not in TT balances; positions carry marks
        "realized_pnl": None,
        "init_margin": _f(getattr(b, "margin_equity", None)),
        "maint_margin": None,
        "account": getattr(a, "account_number", None),
        "currency": "USD",
    }


async def positions() -> list[NormalizedPosition]:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return []
    try:
        rows = await a.get_positions(session, include_marks=True)
    except TypeError:
        rows = await a.get_positions(session)
    except Exception:  # noqa: BLE001
        logger.warning("Positions fetch failed", exc_info=True)
        return []
    out: list[NormalizedPosition] = []
    for p in rows:
        qty = int(p.quantity)
        if getattr(p, "quantity_direction", "") == "Short":
            qty = -abs(qty)
        if qty == 0:
            continue
        direction = "Long" if qty > 0 else "Short"
        out.append(NormalizedPosition(
            symbol=str(p.symbol), quantity=qty, quantity_direction=direction,
            instrument_type=str(getattr(p, "instrument_type", "Equity")),
            avg_price=_f(getattr(p, "average_open_price", 0)) or 0.0,
            account=str(getattr(p, "account_number", ""))))
    return out


async def live_orders() -> list:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return []
    try:
        return list(await a.get_live_orders(session))
    except Exception:  # noqa: BLE001
        return []


async def cancel_order(order_id: int) -> bool:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return False
    try:
        await a.delete_order(session, int(order_id))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Cancel failed for order %s", order_id, exc_info=True)
        return False
```

Also add to `backend/app/config.py`, after `tt_refresh_token`:
```python
    tt_account_number: str = ""      # blank -> first account on the login
```

- [ ] **Step 4: Run to verify it passes**

Same command. Expected: `4 passed`. Also `../.venv/bin/python -c "import app.main; print('main imports OK')"`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/broker backend/app/config.py backend/tests/test_broker_account.py
git commit -m "feat(broker): TT account reads — cached Account, normalized balances/positions"
```

---

### Task 2: Equity orders with the safety gate

**Files:**
- Create: `backend/app/broker/orders.py`
- Test: `backend/tests/test_broker_equity_orders.py`

**Interfaces:**
- Consumes: `app.broker.account.{get_tt_account, positions, live_orders, cancel_order}`; `app.marketdata.facade.stock_quote`; `app.engine.safe_mode.is_safe_mode`; `app.engine.order_store.{record_intent, mark_perm_id}`.
- Produces: `async submit_equity_order(symbol, intent, quantity, *, reason="") -> dict` and module-level indirection points `_account()`, `_session()`, `_mid(symbol)` (tests monkeypatch these). `_INTENT_TO_ACTION` mapping (plain BUY/SELL — equities don't need TO_OPEN/TO_CLOSE).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_equity_orders.py
"""Equity submit: the gate is absolute (paper path may not touch the order API — the
fake RAISES if it does); live path validates with dry_run BEFORE placing; intent→action
mapping; SAFE_MODE blocks. No network."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings


class _Recorder:
    def __init__(self, dry_run_fails=False):
        self.calls = []
        self.dry_run_fails = dry_run_fails

    async def place_order(self, session, order, dry_run=True):
        self.calls.append(("place_order", dry_run,
                           [(l.symbol, str(l.action), int(l.quantity)) for l in order.legs]))
        if dry_run and self.dry_run_fails:
            raise RuntimeError("insufficient buying power")
        return SimpleNamespace(order=SimpleNamespace(id=42))


class _Bomb:
    async def place_order(self, *a, **k):
        raise AssertionError("order API touched in paper mode")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    async def mid(symbol):
        return 101.25
    monkeypatch.setattr(ord_mod, "_mid", mid)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def fake_equity_get(session, symbols):
        sym = list(symbols)[0]
        return [SimpleNamespace(symbol=sym, build_leg=lambda qty, action: SimpleNamespace(
            symbol=sym, action=action, quantity=qty))]
    monkeypatch.setattr(ord_mod, "_equities", fake_equity_get)


@pytest.mark.asyncio
async def test_paper_never_touches_api(monkeypatch):
    async def bomb():
        return _Bomb()
    monkeypatch.setattr(ord_mod, "_account", bomb)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 10, reason="t")
    assert out["transmitted"] is False and out["paper"] is True
    assert out["fill_price"] == 101.25          # simulated at the facade mid


@pytest.mark.asyncio
async def test_paper_no_mid_no_fill(monkeypatch):
    async def bomb():
        return _Bomb()
    monkeypatch.setattr(ord_mod, "_account", bomb)
    async def none_mid(symbol):
        return None
    monkeypatch.setattr(ord_mod, "_mid", none_mid)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 10)
    assert out["transmitted"] is False and out.get("fill_price") is None
    assert "no quote" in out["detail"]


@pytest.mark.asyncio
async def test_live_dry_run_first_then_place(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    rec = _Recorder()
    async def acct():
        return rec
    monkeypatch.setattr(ord_mod, "_account", acct)
    out = await ord_mod.submit_equity_order("AAPL", "exit_short", 5, reason="t")
    assert out["transmitted"] is True and out["order_id"] == 42
    kinds = [(c[0], c[1]) for c in rec.calls]
    assert kinds == [("place_order", True), ("place_order", False)]   # dry-run FIRST
    assert rec.calls[0][2] == [("AAPL", "OrderAction.BUY", 5)]        # exit_short -> BUY


@pytest.mark.asyncio
async def test_live_dry_run_failure_rejects_locally(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    rec = _Recorder(dry_run_fails=True)
    async def acct():
        return rec
    monkeypatch.setattr(ord_mod, "_account", acct)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and "rejected" in out["detail"]
    assert [(c[0], c[1]) for c in rec.calls] == [("place_order", True)]  # never placed


@pytest.mark.asyncio
async def test_safe_mode_blocks(monkeypatch):
    async def safe():
        return True
    monkeypatch.setattr(ord_mod, "is_safe_mode", safe)
    out = await ord_mod.submit_equity_order("AAPL", "enter_long", 5)
    assert out["transmitted"] is False and out.get("safe_mode") is True


@pytest.mark.asyncio
async def test_intent_mapping():
    assert ord_mod.intent_to_action("enter_long") == "BUY"
    assert ord_mod.intent_to_action("exit_short") == "BUY"
    assert ord_mod.intent_to_action("enter_short") == "SELL"
    assert ord_mod.intent_to_action("exit_long") == "SELL"
    with pytest.raises(ValueError):
        ord_mod.intent_to_action("nope")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_be.db && DATABASE_URL=sqlite+aiosqlite:///./_t_be.db ../.venv/bin/python -m pytest tests/test_broker_equity_orders.py -q; rm -f _t_be.db`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/broker/orders.py
"""Order submission against Tastytrade, gated by ENABLE_LIVE_ORDERS (default OFF).

OFF  -> no network order call is constructed; a simulated fill at the sanity-guarded
        facade mid is returned (transmitted: False, paper: True, fill_price=mid). A
        missing/rejected mid means NO fill — never a made-up price.
ON   -> place_order(dry_run=True) validates first (an error rejects locally with the
        reason), then place_order(dry_run=False) transmits for real.

Return shapes match the old app/engine/execution.py exactly so callers don't change.
SDK price convention (verified in tastytrade/order.py): NewOrder.price is a Decimal,
NEGATIVE = debit (pay), POSITIVE = credit (receive); None for market orders."""
from __future__ import annotations

import logging
from decimal import Decimal

from app.config import get_settings
from app.engine.safe_mode import is_safe_mode  # noqa: F401 — monkeypatch point

logger = logging.getLogger("trading.broker.orders")

_INTENT_TO_ACTION = {
    "enter_long": "BUY", "exit_short": "BUY",
    "exit_long": "SELL", "enter_short": "SELL",
}


def intent_to_action(intent: str) -> str:
    try:
        return _INTENT_TO_ACTION[intent]
    except KeyError:
        raise ValueError(f"Unknown order intent: {intent}")


# --- indirection points (tests monkeypatch these) ---------------------------
async def _session():
    from app.tastytrade.session import get_tt_session
    return await get_tt_session()


async def _account():
    from app.broker.account import get_tt_account
    return await get_tt_account()


async def _mid(symbol: str) -> float | None:
    from app.marketdata.facade import stock_quote
    q = await stock_quote(symbol)
    return (q or {}).get("price")


async def _equities(session, symbols):
    from tastytrade.instruments import Equity
    got = await Equity.get(session, symbols)
    return got if isinstance(got, list) else [got]


def _order_action(name: str):
    from tastytrade.order import OrderAction
    return {"BUY": OrderAction.BUY, "SELL": OrderAction.SELL,
            "BUY_TO_OPEN": OrderAction.BUY_TO_OPEN, "BUY_TO_CLOSE": OrderAction.BUY_TO_CLOSE,
            "SELL_TO_OPEN": OrderAction.SELL_TO_OPEN,
            "SELL_TO_CLOSE": OrderAction.SELL_TO_CLOSE}[name]


def _new_order(legs, price: Decimal | None, order_type: str = "MARKET"):
    from tastytrade.order import NewOrder, OrderTimeInForce, OrderType
    return NewOrder(time_in_force=OrderTimeInForce.DAY,
                    order_type=OrderType.LIMIT if order_type == "LIMIT" else OrderType.MARKET,
                    legs=legs, price=price)


async def _validated_place(account, session, order) -> dict:
    """dry_run validation, then the real placement. Local reject on dry-run error."""
    try:
        await account.place_order(session, order, dry_run=True)
    except Exception as exc:  # noqa: BLE001 — reject locally, never place
        logger.error("dry-run rejected: %r", exc)
        return {"transmitted": False, "detail": f"rejected by dry-run: {exc}"}
    placed = await account.place_order(session, order, dry_run=False)
    oid = getattr(getattr(placed, "order", None), "id", None)
    return {"transmitted": True, "order_id": oid}


async def submit_equity_order(symbol: str, intent: str, quantity: int, *,
                              reason: str = "") -> dict:
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    action = intent_to_action(intent)
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({symbol})"}
    s = get_settings()
    if not s.enable_live_orders:
        mid = await _mid(symbol)
        if mid is None:
            logger.warning("[PAPER] %s %s x%d — no quote, no fill", action, symbol, quantity)
            return {"transmitted": False, "paper": True, "fill_price": None,
                    "detail": f"paper {action} {symbol} x{quantity}: no quote, no fill"}
        logger.warning("[PAPER] %s %s x%d @ %.2f (%s) — simulated fill",
                       action, symbol, quantity, mid, reason)
        return {"transmitted": False, "paper": True, "fill_price": mid,
                "detail": f"paper {action} {symbol} x{quantity} @ {mid:.2f}"}
    from app.engine.order_store import mark_perm_id, record_intent
    ref = await record_intent("", symbol, intent, quantity)   # durable BEFORE transmit
    session = await _session()
    account = await _account()
    if session is None or account is None:
        return {"transmitted": False, "detail": "broker unavailable (no TT session)"}
    eq = (await _equities(session, [symbol]))[0]
    leg = eq.build_leg(Decimal(quantity), _order_action(action))
    out = await _validated_place(account, session, _new_order([leg], None))
    if out.get("transmitted") and out.get("order_id"):
        await mark_perm_id(ref, str(out["order_id"]))
    out.setdefault("detail", f"LIVE {action} {symbol} x{quantity}")
    out["ref"] = ref
    logger.warning("[LIVE] %s %s x%d (%s) -> %s", action, symbol, quantity, reason, out["detail"])
    return out
```

- [ ] **Step 4: Run to verify it passes**

Same command. Expected: `6 passed`. Import check OK. Also confirm the SDK price-sign convention (read `.venv/lib/python3.14/site-packages/tastytrade/order.py` NewOrder docstring) and note the finding in your report.

- [ ] **Step 5: Commit**

```bash
git add backend/app/broker/orders.py backend/tests/test_broker_equity_orders.py
git commit -m "feat(broker): gated equity orders — paper fills at guarded mid, dry-run-first live path"
```

---

### Task 3: Brackets (OTOCO) + poll/cancel

**Files:**
- Modify: `backend/app/broker/orders.py` (append)
- Test: `backend/tests/test_broker_bracket.py`

**Interfaces:**
- Produces: `build_otoco(side, quantity, *, target, stop, leg_factory) -> NewComplexOrder` (pure, unit-testable); `async submit_bracket_order(symbol, side, quantity, entry_price, stop, target, *, reason="") -> dict`; `async cancel_symbol_orders(symbol) -> dict`; `async poll_bracket_fill(symbol, ref=None) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_bracket.py
"""OTOCO construction is pure and exhaustively checked (this is where sign/direction
bugs lose money): long -> BUY entry + SELL tp/stop; short mirrored; tp is a LIMIT at
target, stop is a STOP with stop_trigger; paper path returns the same shape the worker
already consumes (transmitted False + stop/target echoed)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings


def _leg_factory(qty, action):
    return SimpleNamespace(symbol="AAPL", action=action, quantity=qty)


def test_build_otoco_long():
    from tastytrade.order import OrderAction, OrderType
    o = ord_mod.build_otoco("long", 10, target=110.0, stop=95.0, leg_factory=_leg_factory)
    assert str(o.trigger_order.legs[0].action) == str(OrderAction.BUY)
    tp, stp = o.orders
    assert tp.order_type == OrderType.LIMIT and float(tp.price) == 110.0
    assert str(tp.legs[0].action) == str(OrderAction.SELL)
    assert stp.order_type == OrderType.STOP and float(stp.stop_trigger) == 95.0
    assert str(stp.legs[0].action) == str(OrderAction.SELL)


def test_build_otoco_short_mirrored():
    from tastytrade.order import OrderAction
    o = ord_mod.build_otoco("short", 5, target=90.0, stop=105.0, leg_factory=_leg_factory)
    assert str(o.trigger_order.legs[0].action) == str(OrderAction.SELL)
    assert all(str(child.legs[0].action) == str(OrderAction.BUY) for child in o.orders)


def test_build_otoco_bad_side():
    with pytest.raises(ValueError):
        ord_mod.build_otoco("sideways", 5, target=1, stop=2, leg_factory=_leg_factory)


@pytest.mark.asyncio
async def test_paper_bracket_shape(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def mid(symbol):
        return 100.0
    monkeypatch.setattr(ord_mod, "_mid", mid)
    out = await ord_mod.submit_bracket_order("AAPL", "long", 10, 100.0, 95.0, 110.0)
    assert out["transmitted"] is False and out["paper"] is True and out["bracket"] is True
    assert out["stop"] == 95.0 and out["target"] == 110.0


@pytest.mark.asyncio
async def test_paper_poll_and_cancel(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    assert await ord_mod.poll_bracket_fill("AAPL") is None      # worker simulates fills
    out = await ord_mod.cancel_symbol_orders("AAPL")
    assert out == {"cancelled": 0, "paper": True}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_bb.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bb.db ../.venv/bin/python -m pytest tests/test_broker_bracket.py -q; rm -f _t_bb.db`
Expected: FAIL — `build_otoco` undefined.

- [ ] **Step 3: Implement (append to `backend/app/broker/orders.py`)**

```python
def build_otoco(side: str, quantity: int, *, target: float, stop: float, leg_factory):
    """OTOCO: market entry triggers an OCO pair (take-profit LIMIT + STOP). Pure —
    `leg_factory(qty, action)` supplies legs so this is unit-testable without the SDK
    instruments. Sign convention: tp price is a CREDIT (positive) — closing a long
    receives money; the SDK validates the pair server-side on placement."""
    from tastytrade.order import (NewComplexOrder, NewOrder, OrderAction,
                                  OrderTimeInForce, OrderType)
    if side == "long":
        entry_action, close_action = OrderAction.BUY, OrderAction.SELL
    elif side == "short":
        entry_action, close_action = OrderAction.SELL, OrderAction.BUY
    else:
        raise ValueError(f"Unknown side: {side!r}")
    q = Decimal(quantity)
    entry = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET,
                     legs=[leg_factory(q, entry_action)])
    tp = NewOrder(time_in_force=OrderTimeInForce.GTC, order_type=OrderType.LIMIT,
                  legs=[leg_factory(q, close_action)], price=Decimal(str(target)))
    stp = NewOrder(time_in_force=OrderTimeInForce.GTC, order_type=OrderType.STOP,
                   legs=[leg_factory(q, close_action)], stop_trigger=Decimal(str(stop)))
    return NewComplexOrder(trigger_order=entry, orders=[tp, stp])


async def submit_bracket_order(symbol: str, side: str, quantity: int, entry_price: float,
                               stop: float, target: float, *, reason: str = "") -> dict:
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    if side not in ("long", "short"):
        raise ValueError(f"Unknown side: {side!r}")
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({symbol})"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] BRACKET %s %s x%d entry~%.2f stop %.2f target %.2f (%s)",
                       side, symbol, quantity, entry_price, stop, target, reason)
        return {"transmitted": False, "paper": True, "bracket": True,
                "detail": f"paper bracket {side} {symbol} x{quantity} "
                          f"(stop {stop:.2f} / target {target:.2f})",
                "stop": stop, "target": target}
    session = await _session()
    account = await _account()
    if session is None or account is None:
        return {"transmitted": False, "detail": "broker unavailable (no TT session)"}
    eq = (await _equities(session, [symbol]))[0]
    combo = build_otoco(side, quantity, target=target, stop=stop, leg_factory=eq.build_leg)
    try:
        await account.place_complex_order(session, combo, dry_run=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("bracket dry-run rejected: %r", exc)
        return {"transmitted": False, "detail": f"rejected by dry-run: {exc}"}
    placed = await account.place_complex_order(session, combo, dry_run=False)
    coid = getattr(getattr(placed, "complex_order", None), "id", None)
    logger.warning("[LIVE BRACKET] %s %s x%d stop %.2f target %.2f (%s)",
                   side, symbol, quantity, stop, target, reason)
    return {"transmitted": True, "bracket": True, "parent_order_id": coid,
            "detail": f"LIVE bracket {side} {symbol} x{quantity}",
            "stop": stop, "target": target}


async def cancel_symbol_orders(symbol: str) -> dict:
    s = get_settings()
    if not s.enable_live_orders:
        return {"cancelled": 0, "paper": True}
    from app.broker.account import cancel_order, live_orders
    cancelled = 0
    for o in await live_orders():
        legs = getattr(o, "legs", []) or []
        if any(getattr(l, "symbol", "").split()[0] == symbol for l in legs):
            if await cancel_order(getattr(o, "id", 0)):
                cancelled += 1
    return {"cancelled": cancelled}


async def poll_bracket_fill(symbol: str, ref=None) -> dict | None:
    """None while still holding; else {price, ms, reason} for the filled closer.
    Paper mode -> None (the worker simulates fills on 1m bars, unchanged)."""
    s = get_settings()
    if not s.enable_live_orders:
        return None
    from app.broker.account import get_tt_account, positions
    for p in await positions():
        if p.symbol == symbol and p.quantity != 0:
            return None
    account = await get_tt_account()
    session = await _session()
    if account is None or session is None:
        return None
    try:
        history = await account.get_order_history(session, per_page=50)
    except Exception:  # noqa: BLE001
        return {"price": None, "ms": None, "reason": "bracket"}
    from tastytrade.order import OrderType
    for o in history:
        legs = getattr(o, "legs", []) or []
        if not any(getattr(l, "symbol", "").split()[0] == symbol for l in legs):
            continue
        if str(getattr(o, "status", "")) not in ("OrderStatus.FILLED", "Filled"):
            continue
        if o.order_type == OrderType.STOP:
            return {"price": float(o.stop_trigger or 0), "ms": None, "reason": "stop"}
        if o.order_type == OrderType.LIMIT:
            return {"price": abs(float(o.price or 0)), "ms": None, "reason": "target"}
    return {"price": None, "ms": None, "reason": "bracket"}
```

- [ ] **Step 4: Run to verify it passes**

Same command. Expected: `5 passed` (this file) — and re-run Task 2's file too (same module): both green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/broker/orders.py backend/tests/test_broker_bracket.py
git commit -m "feat(broker): OTOCO brackets + poll/cancel over Tastytrade"
```

---

### Task 4: Multi-leg option combos (Triple Calendar)

**Files:**
- Modify: `backend/app/broker/orders.py` (append)
- Test: `backend/tests/test_broker_combo.py`

**Interfaces:**
- Consumes: chain streamer-symbols via `app.marketdata.facade._tt_chain` (maps `(iso, strike, right) -> streamer_symbol`); `Option.streamer_symbol_to_occ`; `Option.get(session, occ_symbol)`.
- Produces: `occ_symbol_for(underlying, expiry, strike, right) -> str` (pure; OCC padding rules); `async submit_combo_order(underlying, legs, *, net_price=None, action="BUY", reason="", quantity=1) -> dict` — legs are the TC's `{expiry(date), strike, right, action}` dicts; TO_OPEN/TO_CLOSE chosen from current TT positions.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_combo.py
"""Combo submit: OCC symbol construction (exact padding), per-leg TO_OPEN/TO_CLOSE from
the positions lookup, net-debit price sign (negative Decimal = pay), paper path never
touches the API."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.config import get_settings


def test_occ_symbol_padding():
    # OCC: 6-char root, yymmdd, C/P, strike*1000 as 8 digits.
    assert ord_mod.occ_symbol_for("QQQ", date(2026, 8, 7), 725.0, "C") == "QQQ   260807C00725000"
    assert ord_mod.occ_symbol_for("SPY", date(2026, 12, 18), 82.5, "P") == "SPY   261218P00082500"
    assert ord_mod.occ_symbol_for("BRKB", date(2026, 8, 7), 1000.0, "C") == "BRKB  260807C01000000"


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    rec = SimpleNamespace(calls=[])
    async def place_order(session, order, dry_run=True):
        rec.calls.append((dry_run, [(l.symbol, str(l.action), int(l.quantity)) for l in order.legs],
                          order.price))
        return SimpleNamespace(order=SimpleNamespace(id=7))
    async def acct():
        return SimpleNamespace(place_order=place_order)
    monkeypatch.setattr(ord_mod, "_account", acct)
    async def fake_options(session, occ):
        return SimpleNamespace(symbol=occ, build_leg=lambda qty, action: SimpleNamespace(
            symbol=occ, action=action, quantity=qty))
    monkeypatch.setattr(ord_mod, "_option", fake_options)
    async def held():
        return {"QQQ   260807C00725000"}      # already long this one
    monkeypatch.setattr(ord_mod, "_held_symbols", held)
    return rec


@pytest.mark.asyncio
async def test_combo_actions_and_price(wired):
    legs = [
        {"expiry": date(2026, 8, 7), "strike": 725.0, "right": "C", "action": "SELL"},
        {"expiry": date(2026, 9, 4), "strike": 725.0, "right": "C", "action": "BUY"},
    ]
    out = await ord_mod.submit_combo_order("QQQ", legs, net_price=1.85, action="BUY",
                                           quantity=2)
    assert out["transmitted"] is True and out["legs"] == 2 and out["order_id"] == 7
    dry, placed = wired.calls
    assert dry[0] is True and placed[0] is False                 # dry-run first
    (s1, a1, q1), (s2, a2, q2) = placed[1]
    assert s1 == "QQQ   260807C00725000" and "SELL_TO_CLOSE" in a1 and q1 == 2  # held -> TO_CLOSE
    assert s2 == "QQQ   260904C00725000" and "BUY_TO_OPEN" in a2 and q2 == 2    # not held -> TO_OPEN
    assert placed[2] == Decimal("-1.85")     # net debit -> negative (pay)


@pytest.mark.asyncio
async def test_combo_paper_never_touches_api(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    async def no_safe():
        return False
    monkeypatch.setattr(ord_mod, "is_safe_mode", no_safe)
    async def bomb():
        raise AssertionError("touched")
    monkeypatch.setattr(ord_mod, "_account", bomb)
    out = await ord_mod.submit_combo_order("QQQ", [
        {"expiry": date(2026, 8, 7), "strike": 725.0, "right": "C", "action": "BUY"}],
        net_price=2.0)
    assert out["transmitted"] is False and out["paper"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && rm -f _t_bc.db && DATABASE_URL=sqlite+aiosqlite:///./_t_bc.db ../.venv/bin/python -m pytest tests/test_broker_combo.py -q; rm -f _t_bc.db`
Expected: FAIL.

- [ ] **Step 3: Implement (append to `backend/app/broker/orders.py`)**

```python
def occ_symbol_for(underlying: str, expiry, strike: float, right: str) -> str:
    """OCC option symbol: 6-char padded root + yymmdd + C/P + strike*1000 zero-padded
    to 8 digits. This is the Tastytrade instrument symbol format for options."""
    root = f"{underlying.upper():<6}"
    ymd = expiry.strftime("%y%m%d")
    k = f"{int(round(float(strike) * 1000)):08d}"
    return f"{root}{ymd}{right.upper()}{k}"


async def _option(session, occ_symbol: str):
    from tastytrade.instruments import Option
    return await Option.get(session, occ_symbol)


async def _held_symbols() -> set[str]:
    from app.broker.account import positions
    return {p.symbol for p in await positions()}


async def submit_combo_order(underlying: str, legs: list[dict], *, net_price: float | None = None,
                             action: str = "BUY", reason: str = "", quantity: int = 1) -> dict:
    """Multi-leg options order. Each leg = {expiry(date), strike, right, action}.
    Per-leg OrderAction: (BUY|SELL) + (TO_CLOSE if the account currently holds that OCC
    symbol on the closing side, else TO_OPEN). Net price: combo LIMIT at the (sanity-
    guarded) net, sign per SDK convention — `action=BUY` (opening for a debit) -> pay ->
    NEGATIVE Decimal; `action=SELL` (closing for a credit) -> receive -> POSITIVE."""
    if not legs:
        return {"transmitted": False, "detail": "no legs"}
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({underlying})"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] COMBO %s %s x%d legs net~%s (%s)",
                       action, underlying, len(legs), net_price, reason)
        return {"transmitted": False, "paper": True, "legs": len(legs),
                "detail": f"paper combo {action} {underlying} {len(legs)} legs"}
    session = await _session()
    account = await _account()
    if session is None or account is None:
        return {"transmitted": False, "detail": "broker unavailable (no TT session)"}
    held = await _held_symbols()
    q = Decimal(max(1, int(quantity)))
    sdk_legs = []
    for spec in legs:
        occ = occ_symbol_for(underlying, spec["expiry"], spec["strike"], spec["right"])
        inst = await _option(session, occ)
        buying = spec["action"].upper() == "BUY"
        closing = occ in held    # holding it and now trading the opposite way = closing
        name = ("BUY_TO_CLOSE" if closing else "BUY_TO_OPEN") if buying else \
               ("SELL_TO_CLOSE" if closing else "SELL_TO_OPEN")
        sdk_legs.append(inst.build_leg(q, _order_action(name)))
    price = None
    if net_price is not None:
        p = Decimal(str(round(abs(net_price), 2)))
        price = -p if action.upper() == "BUY" else p
    order = _new_order(sdk_legs, price, order_type="LIMIT" if price is not None else "MARKET")
    out = await _validated_place(account, session, order)
    out["legs"] = len(legs)
    out.setdefault("detail",
                   f"LIVE combo {action} {underlying} {len(legs)} legs")
    logger.warning("[LIVE COMBO] %s %s %d legs net %s (%s) -> %s",
                   action, underlying, len(legs), net_price, reason, out["detail"])
    return out
```

- [ ] **Step 4: Run to verify it passes**

Same command. Expected: `3 passed`. Re-run Tasks 2–3 test files too (same module) — all green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/broker/orders.py backend/tests/test_broker_combo.py
git commit -m "feat(broker): multi-leg option combos — OCC symbols, TO_OPEN/TO_CLOSE, signed net price"
```

---

### Task 5: `flatten_all` (kill switch)

**Files:**
- Modify: `backend/app/broker/orders.py` (append)
- Test: `backend/tests/test_broker_flatten.py`

**Interfaces:**
- Produces: `async flatten_all() -> str` — cancel every live order, then market-close every position (equity plain SELL/BUY; options SELL_TO_CLOSE/BUY_TO_CLOSE). Paper mode: logs would-close per position and RAISES `RuntimeError` listing them as not-flat (identical honesty contract to the IBKR version — the kill switch reports `fully_flat=False`). Live failures collect and raise; успех returns the summary string.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_flatten.py
"""Kill switch: cancel-then-close ordering; equity vs option close actions; paper mode
raises (honest not-flat) and touches no order API; live failures raise with the list."""
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.broker.account import NormalizedPosition
from app.config import get_settings


def _pos(symbol, qty, itype="Equity"):
    return NormalizedPosition(symbol=symbol, quantity=qty,
                              quantity_direction="Long" if qty > 0 else "Short",
                              instrument_type=itype, avg_price=1.0, account="X")


@pytest.fixture
def wired(monkeypatch):
    calls = SimpleNamespace(cancelled=[], placed=[])
    async def live_orders():
        return [SimpleNamespace(id=1, legs=[SimpleNamespace(symbol="AAPL")])]
    async def cancel(oid):
        calls.cancelled.append(oid)
        return True
    monkeypatch.setattr(ord_mod, "_live_orders", live_orders)
    monkeypatch.setattr(ord_mod, "_cancel_order", cancel)
    async def poss():
        return [_pos("AAPL", 10), _pos("QQQ   260807C00725000", -2, "Equity Option")]
    monkeypatch.setattr(ord_mod, "_positions", poss)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    async def place_order(session, order, dry_run=True):
        calls.placed.append((dry_run, [(l.symbol, str(l.action), int(l.quantity))
                                       for l in order.legs]))
        return SimpleNamespace(order=SimpleNamespace(id=9))
    async def acct():
        return SimpleNamespace(place_order=place_order)
    monkeypatch.setattr(ord_mod, "_account", acct)
    async def fake_equity(session, symbols):
        sym = list(symbols)[0]
        return [SimpleNamespace(symbol=sym, build_leg=lambda qty, action: SimpleNamespace(
            symbol=sym, action=action, quantity=qty))]
    monkeypatch.setattr(ord_mod, "_equities", fake_equity)
    async def fake_option(session, occ):
        return SimpleNamespace(symbol=occ, build_leg=lambda qty, action: SimpleNamespace(
            symbol=occ, action=action, quantity=qty))
    monkeypatch.setattr(ord_mod, "_option", fake_option)
    return calls


@pytest.mark.asyncio
async def test_live_flatten_cancels_then_closes(wired, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    msg = await ord_mod.flatten_all()
    assert wired.cancelled == [1]
    # closes are market orders placed for real (dry_run False), correct directions:
    placed = [p for p in wired.placed if p[0] is False]
    acts = {p[1][0][0]: p[1][0][1] for p in placed}
    assert "SELL" in acts["AAPL"]                                # long equity -> SELL
    assert "BUY_TO_CLOSE" in acts["QQQ   260807C00725000"]       # short option -> BUY_TO_CLOSE
    assert "Flattened 2" in msg


@pytest.mark.asyncio
async def test_paper_flatten_raises_honest(monkeypatch, wired):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert "not flat" in str(ei.value)
    assert wired.placed == []            # no order API in paper mode
```

- [ ] **Step 2: Run to verify it fails** (same command pattern, `_t_bf.db`). Expected: FAIL.

- [ ] **Step 3: Implement (append to `backend/app/broker/orders.py`)**

```python
async def _positions():
    from app.broker.account import positions
    return await positions()


async def _live_orders():
    from app.broker.account import live_orders
    return await live_orders()


async def _cancel_order(order_id):
    from app.broker.account import cancel_order
    return await cancel_order(order_id)


async def flatten_all() -> str:
    """Cancel every live order, then market-close every open position. Live: transmits
    closing orders (equity SELL/BUY; options *_TO_CLOSE). Paper: logs would-close and
    raises so the kill switch honestly reports fully_flat=False. Raises when anything
    is left standing."""
    s = get_settings()
    poss = await _positions()
    cancelled = 0
    closed: list[str] = []
    failures: list[str] = []

    if s.enable_live_orders:
        for o in await _live_orders():
            try:
                if await _cancel_order(getattr(o, "id", 0)):
                    cancelled += 1
            except Exception as exc:  # noqa: BLE001 — record, keep killing
                failures.append(f"order-cancel {getattr(o, 'id', '?')}: {exc}")

    total = len(poss)
    session = await _session() if s.enable_live_orders else None
    account = await _account() if s.enable_live_orders else None
    for p in poss:
        qty = abs(p.quantity)
        if qty <= 0:
            continue
        if not s.enable_live_orders:
            logger.warning("[FLATTEN-PAPER] would close %s x%d — ENABLE_LIVE_ORDERS off",
                           p.symbol, qty)
            failures.append(f"{p.symbol} x{qty} (paper: not transmitted)")
            continue
        if session is None or account is None:
            failures.append(f"{p.symbol} (broker unavailable)")
            continue
        try:
            is_option = "Option" in (p.instrument_type or "")
            if is_option:
                inst = await _option(session, p.symbol)
                name = "SELL_TO_CLOSE" if p.quantity > 0 else "BUY_TO_CLOSE"
            else:
                inst = (await _equities(session, [p.symbol]))[0]
                name = "SELL" if p.quantity > 0 else "BUY"
            leg = inst.build_leg(Decimal(qty), _order_action(name))
            placed = await account.place_order(session, _new_order([leg], None),
                                               dry_run=False)   # kill switch: no dry-run delay
            _ = placed
            logger.warning("[FLATTEN-LIVE] %s %s x%d", name, p.symbol, qty)
            closed.append(f"{p.symbol} x{qty}")
        except Exception as exc:  # noqa: BLE001 — must attempt every position
            logger.error("Flatten failed for %s: %r", p.symbol, exc)
            failures.append(f"{p.symbol} ({exc})")

    note = f"cancelled {cancelled} resting order(s); " if cancelled else ""
    if total == 0 and not failures:
        return f"{note}No open positions." if note else "No open positions."
    if failures:
        raise RuntimeError(f"Flatten incomplete: {note}closed {len(closed)}/{total} "
                           f"[{', '.join(closed) or 'none'}]; not flat: {', '.join(failures)}")
    return f"{note}Flattened {total} position(s): {', '.join(closed)}"
```

- [ ] **Step 4: Run to verify it passes** — this file + Tasks 2–4 files, all green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/broker/orders.py backend/tests/test_broker_flatten.py
git commit -m "feat(broker): flatten_all kill switch — cancel then close, honest paper mode"
```

---

### Task 6: Strangler swap — `engine/execution.py` delegates to the broker

**Files:**
- Modify: `backend/app/engine/execution.py` (full replace)
- Test: existing suite (worker/TC/dashboard tests exercise the seam)

**Interfaces:**
- Produces: same module path re-exporting the six functions from `app.broker.orders`. NOTHING else changes — `worker.py`, `strategy_class.py`, `routers/dashboard.py` keep their imports.

- [ ] **Step 1: Replace `backend/app/engine/execution.py` with:**

```python
"""Order execution + flatten — now served by the Tastytrade broker layer.

This module keeps its historical import path and public API (the worker, Triple
Calendar executor, kill switch, and routers all import from here); the implementation
lives in app/broker/orders.py. ENABLE_LIVE_ORDERS gating, SAFE_MODE blocking, and
return-dict shapes are preserved there verbatim."""
from __future__ import annotations

from app.broker.orders import (cancel_symbol_orders, flatten_all,  # noqa: F401
                               poll_bracket_fill, submit_bracket_order,
                               submit_combo_order, submit_equity_order)

__all__ = ["submit_equity_order", "submit_bracket_order", "cancel_symbol_orders",
           "poll_bracket_fill", "flatten_all", "submit_combo_order"]
```

- [ ] **Step 2: Run the FULL suite + standalone TC tests**

```bash
cd backend && rm -f _t_sw.db && DATABASE_URL=sqlite+aiosqlite:///./_t_sw.db ../.venv/bin/python -m pytest tests/ -q 2>&1 | tail -3; rm -f _t_sw.db
for t in test_tc_monitor_only test_tc_multi_live test_tc_vix_live test_tc_contracts_live test_tc_roll_live test_tc_live_persist; do
  rm -f _t_$t.db; echo -n "$t: "
  DATABASE_URL=sqlite+aiosqlite:///./_t_$t.db ../.venv/bin/python -m tests.$t 2>&1 | grep -cE "ALL .* PASSED" | sed 's/^1$/PASS/;s/^0$/FAIL/'; rm -f _t_$t.db
done
```
Expected: everything green. If a test monkeypatched `app.engine.execution.<internal>` (e.g. old IBKR helpers), fix the TEST's patch target to `app.broker.orders`, never weaken assertions; list every such repoint in your report.

- [ ] **Step 3: Commit**

```bash
git add backend/app/engine/execution.py backend/tests
git commit -m "refactor(engine): execution.py delegates to the Tastytrade broker layer"
```

---

### Task 7: Reconciliation + recovery without the Gateway

**Files:**
- Modify: `backend/app/engine/reconciliation.py` (replace `fetch_snapshot`), `backend/app/engine/recovery.py` (gateway-free), `backend/app/main.py` (startup hook)
- Test: `backend/tests/test_broker_reconciliation.py` (+ existing recon tests keep passing)

**Interfaces:**
- Produces: `async fetch_snapshot() -> dict` (no `ib` arg; positions from `app.broker.account.positions()`, orders from `live_orders()`, fills from order history — same `BrokerPosition/BrokerFill/BrokerOrder` dataclasses); `reconcile(...)` unchanged (pure); `async run_recovery() -> bool` (no args): fetch → reconcile vs `local_open_positions()` → mismatch trips SAFE_MODE + alert (exact current behavior), clean → journal "Recovery clean — READY" + schedule backfill. `main.py` lifespan calls `run_recovery()` once at startup instead of the gateway connect.
- Note: recovery equity-only like today (`local_open_positions` reads TradeRecord); TC option records are reconciled by their own store — out of scope, unchanged.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_reconciliation.py
"""Broker-sourced snapshot + gateway-free recovery: mismatch trips SAFE_MODE; clean run
journals READY. reconcile() itself is unchanged and already covered."""
from types import SimpleNamespace

import pytest

from app.broker.account import NormalizedPosition
from app.engine import recovery as rec_mod
from app.engine import reconciliation as recon_mod
from app.engine.safe_mode import clear_safe_mode, is_safe_mode


def _pos(sym, qty):
    return NormalizedPosition(symbol=sym, quantity=qty, quantity_direction="Long",
                              instrument_type="Equity", avg_price=1.0, account="X")


@pytest.mark.asyncio
async def test_fetch_snapshot_from_broker(monkeypatch):
    async def poss():
        return [_pos("AAPL", 10)]
    async def orders():
        return [SimpleNamespace(id=5, status="Live",
                                legs=[SimpleNamespace(symbol="AAPL")])]
    monkeypatch.setattr(recon_mod, "_positions", poss)
    monkeypatch.setattr(recon_mod, "_live_orders", orders)
    snap = await recon_mod.fetch_snapshot()
    assert snap["positions"][0].symbol == "AAPL" and snap["positions"][0].qty == 10
    assert snap["orders"][0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_recovery_mismatch_trips_safe_mode(monkeypatch):
    from app.db.base import init_db
    await init_db()
    await clear_safe_mode()
    async def snap():
        return {"positions": [recon_mod.BrokerPosition("AAPL", 10, 1.0)],
                "fills": [], "orders": []}
    monkeypatch.setattr(rec_mod, "fetch_snapshot", snap)
    async def local():
        return {}                       # broker holds AAPL, we think we're flat
    monkeypatch.setattr(rec_mod, "local_open_positions", local)
    sent = []
    async def alert(kind, reason, meta=None):
        sent.append(kind)
    monkeypatch.setattr("app.services.alerts.send_alert", alert)
    ok = await rec_mod.run_recovery()
    assert ok is False and await is_safe_mode() is True and "safe_mode" in sent
    await clear_safe_mode()


@pytest.mark.asyncio
async def test_recovery_clean(monkeypatch):
    from app.db.base import init_db
    await init_db()
    await clear_safe_mode()
    async def snap():
        return {"positions": [], "fills": [], "orders": []}
    monkeypatch.setattr(rec_mod, "fetch_snapshot", snap)
    async def local():
        return {}
    monkeypatch.setattr(rec_mod, "local_open_positions", local)
    assert await rec_mod.run_recovery() is True
    assert await is_safe_mode() is False
```

- [ ] **Step 2: Run to verify it fails** (`_t_br.db`). Expected: FAIL (`fetch_snapshot` still takes `ib`).

- [ ] **Step 3: Implement**

`backend/app/engine/reconciliation.py` — delete the `ib_async` import and replace `fetch_snapshot` with:

```python
async def _positions():
    from app.broker.account import positions
    return await positions()


async def _live_orders():
    from app.broker.account import live_orders
    return await live_orders()


async def fetch_snapshot() -> dict:
    """Broker truth from Tastytrade: positions + live orders (fills come with order
    history and are not needed for the position reconcile — kept as an empty list to
    preserve the snapshot shape)."""
    positions = [BrokerPosition(p.symbol, p.quantity, p.avg_price)
                 for p in await _positions() if p.quantity != 0]
    orders = []
    for o in await _live_orders():
        legs = getattr(o, "legs", []) or []
        sym = getattr(legs[0], "symbol", "?") if legs else "?"
        orders.append(BrokerOrder(sym, str(getattr(o, "id", "")) or None, None,
                                  str(getattr(o, "status", ""))))
    return {"positions": positions, "fills": [], "orders": orders}
```

`backend/app/engine/recovery.py` — full replace:

```python
"""Deterministic recovery: reconcile broker truth vs local records at startup (and on
demand). Mismatch -> SAFE_MODE until a human clears it; clean -> READY + backfill."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import TradeRecord
from app.engine.reconciliation import fetch_snapshot, reconcile
from app.engine.safe_mode import trip_safe_mode
from app.services.journal import log_event

logger = logging.getLogger("trading.recovery")


async def local_open_positions() -> dict[str, int]:
    out: dict[str, int] = {}
    async with session_scope() as s:
        rows = (await s.execute(
            select(TradeRecord).where(TradeRecord.status == "open"))).scalars().all()
        for r in rows:
            signed = int(r.quantity) * (1 if (r.side or "").lower() in ("buy", "long") else -1)
            out[r.symbol] = out.get(r.symbol, 0) + signed
    return {k: v for k, v in out.items() if v != 0}


async def run_recovery() -> bool:
    snap = await fetch_snapshot()
    result = reconcile(snap["positions"], await local_open_positions())
    if not result.ok:
        reason = "; ".join(result.discrepancies)
        await trip_safe_mode(reason)
        await log_event(f"SAFE MODE on recovery: {reason}", level="error", category="system")
        from app.services.alerts import send_alert
        await send_alert("safe_mode", reason, {"discrepancies": result.discrepancies})
        return False
    _schedule_backfill()
    await log_event("Recovery clean — READY", category="system")
    return True


def _schedule_backfill() -> None:
    import asyncio

    from app.services.backfill import backfill_active
    asyncio.create_task(backfill_active())
```

`backend/app/main.py` lifespan — replace the gateway-connect block (`from app.ibkr.gateway import get_gateway` … "workers stay paused until READY") with:

```python
    from app.engine.recovery import run_recovery
    try:
        await run_recovery()
    except Exception:  # noqa: BLE001 — recovery is best-effort at boot
        logger.warning("startup recovery failed", exc_info=True)
```

- [ ] **Step 4: Run** the new file + the whole suite. If existing recovery/recon tests call `run_recovery(ib, gateway)` / `fetch_snapshot(ib)`, update those TESTS to the new no-arg signatures (they were testing the same behaviors); list repoints in your report.

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/reconciliation.py backend/app/engine/recovery.py backend/app/main.py backend/tests
git commit -m "feat(engine): broker-sourced reconciliation + gateway-free recovery at startup"
```

---

### Task 8: Auth/status swap — probe-backed readiness

**Files:**
- Create: `backend/app/broker/status.py`
- Modify: `backend/app/auth/manager.py`, `backend/app/auth/gate.py`, `backend/app/auth/routes.py`
- Test: `backend/tests/test_broker_status.py`

**Interfaces:**
- Produces: `app/broker/status.py` with `async is_ready() -> bool` (probe ok, cached — moves `_probe_tt` + `_tt_probe` cache OUT of `auth/routes.py` into here as `probe(force=False)`), `def is_ready_cached() -> bool` (sync read of the last probe result, False when never probed — for sync call sites like `gate.is_authenticated`). Background: main.py lifespan starts a 60s probe refresher task so the cached value stays fresh.
- `auth/manager.py`: `is_authenticated()` → `is_ready_cached()`; `connect()` → force probe; `disconnect()` → no-op (kept for API compat).
- `auth/routes.py`: `/status` reports `{connected: <probe ok>, detail: <probe error or None>, broker: "tastytrade", account: <masked number>}` (keep the ConnectionStatus model fields it already has — set `host/port/client_id` to None; they disappear from the UI in Task 13). `/connect` forces a probe. `tt_status`/`tt_save` import `probe` from `app.broker.status` (single source).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_broker_status.py
"""Probe-backed readiness: cached result, force refresh, sync read for gates."""
import pytest

from app.broker import status as st


@pytest.mark.asyncio
async def test_probe_cached_and_forced(monkeypatch):
    calls = []
    async def fake_refresh(force):
        calls.append(force)
        return "ok"
    monkeypatch.setattr(st, "_do_probe", fake_refresh)
    st._cache.update(result=None, at=0.0)
    assert await st.probe() == "ok"          # cold -> probes
    assert await st.probe() == "ok"          # warm -> cached (no second call)
    assert calls == [False]
    assert await st.probe(force=True) == "ok"
    assert calls == [False, True]
    assert st.is_ready_cached() is True


@pytest.mark.asyncio
async def test_rejected_probe_not_ready(monkeypatch):
    async def bad(force):
        return "rejected: invalid_grant"
    monkeypatch.setattr(st, "_do_probe", bad)
    st._cache.update(result=None, at=0.0)
    await st.probe()
    assert st.is_ready_cached() is False
```

- [ ] **Step 2: Run to verify it fails** (`_t_bs2.db`). Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# backend/app/broker/status.py
"""Broker readiness = the Tastytrade OAuth pair verifies (token refresh probe).
Single source of truth for the auth gate, /status, the tt card, and strategy pause
conditions. Cached 5 min; a background refresher (main.py lifespan) keeps the sync
`is_ready_cached()` view fresh for non-async call sites."""
from __future__ import annotations

import time

_cache: dict = {"result": None, "at": 0.0}
_TTL = 300.0


async def _do_probe(force: bool) -> str | None:
    from app.config import get_settings
    s = get_settings()
    if not (s.tt_provider_secret and s.tt_refresh_token):
        return None
    try:
        from app.tastytrade.session import get_tt_session
        sess = await get_tt_session()
        if sess is None:
            return None
        await sess.refresh(force=True)
        return "ok"
    except Exception as e:  # noqa: BLE001 — surfaced, not raised
        return f"rejected: {str(e)[:200]}"


async def probe(force: bool = False) -> str | None:
    if not force and _cache["result"] is not None \
            and time.monotonic() - _cache["at"] < _TTL:
        return _cache["result"]
    result = await _do_probe(force)
    _cache.update(result=result, at=time.monotonic())
    return result


async def is_ready() -> bool:
    return (await probe()) == "ok"


def is_ready_cached() -> bool:
    return _cache["result"] == "ok"


async def refresher() -> None:
    """Lifespan task: keep the cached probe fresh (every 60s)."""
    import asyncio
    while True:
        try:
            await probe()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(60)
```

`auth/manager.py` — replace the class body: `is_authenticated()` returns `is_ready_cached()`; `connect()` `await probe(force=True)` and return `is_ready_cached()`; `disconnect()` no-op with a comment. `auth/gate.py` — only the docstring/401 detail wording changes ("Tastytrade credentials not verified"). `auth/routes.py` — delete `_tt_probe`/`_probe_tt` (import `probe` from `app.broker.status`); `_status()` builds ConnectionStatus from the probe (`connected=is_ready_cached()`, detail = probe error text or None, host/port/client_id=None, accounts=[masked `tt_account_number` or the cached account number]). `main.py` lifespan: `asyncio.create_task(refresher())` next to the builder monitor.

- [ ] **Step 4: Run** new tests + full suite (auth-dependent router tests must still pass — where they faked `session_manager.is_authenticated`, behavior is unchanged since the seam survives).

- [ ] **Step 5: Commit**

```bash
git add backend/app/broker/status.py backend/app/auth backend/app/main.py backend/tests/test_broker_status.py
git commit -m "feat(broker): probe-backed readiness replaces Gateway auth"
```

---

### Task 9: Dashboard, WS, market stream

**Files:**
- Modify: `backend/app/routers/dashboard.py`, `backend/app/routers/ws.py` (import swap: `app.ibkr.account` → `app.broker.account`), `backend/app/services/market_stream.py` (full replace)
- Test: `backend/tests/test_market_stream_tt.py` + existing dashboard tests

**Interfaces:**
- `dashboard.py`/`ws.py`: `get_account_summary()` → `balances()`, `get_positions()` → `positions()` (same shapes — mechanical import swap; keep local alias names so diffs stay small).
- `market_stream.py`: same public surface (`market_stream` singleton, `ensure_started()`, `overview()`), but the loop subscribes the overview symbols on the DXLink streamer once and `overview()` reads the streamer's quote/trade caches (`get_streamer()._quotes/._trades`) — no ib_async. The task test fakes the streamer exactly like `tests/test_tt_streamer.py` does.

- [ ] **Step 1: Test** (`backend/tests/test_market_stream_tt.py`):

```python
"""Market stream over DXLink: ensure_started subscribes overview symbols; overview()
reads the streamer caches synchronously."""
import pytest

from app.services import market_stream as ms


@pytest.mark.asyncio
async def test_overview_reads_streamer_cache(monkeypatch):
    class _FakeStreamer:
        _quotes = {"SPY": {"bid": 500.0, "ask": 500.2}}
        _trades = {"SPY": 500.1}
        async def quote(self, symbol, timeout=3.0):
            return self._quotes.get(symbol)
        async def trade_price(self, symbol, timeout=3.0):
            return self._trades.get(symbol)
    monkeypatch.setattr(ms, "_streamer", lambda: _FakeStreamer())
    rows = ms.market_stream.overview()
    spy = next((r for r in rows if r["symbol"] == "SPY"), None)
    assert spy is not None and spy["last"] == 500.1
```

- [ ] **Step 2–3: Implement.** Replace `market_stream.py`'s ib_async loop: `_streamer()` indirection returning `get_streamer()`; `ensure_started()` spawns a task that calls `quote()`/`trade_price()` once per overview symbol (subscribing them), then exits (the pumps keep the caches fresh); `overview()` builds the same row shape as today from `_streamer()._quotes/._trades` via `index_row` (keep whatever shape `services/market.py.index_row` needs — read it and match). Keep the "restart lazily on ws connect" behavior trivially (task is idempotent).

- [ ] **Step 4: Run** new test + full suite + `python -c "import app.main"`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/dashboard.py backend/app/routers/ws.py backend/app/services/market_stream.py backend/tests/test_market_stream_tt.py
git commit -m "feat(dashboard): account/positions/stream served by Tastytrade"
```

---

### Task 10: Strategy pause conditions — probe instead of Gateway

**Files:**
- Modify: `backend/app/strategies/triple_calendar/strategy_class.py`, `backend/app/options/monitor.py`, `backend/app/engine/worker.py` (pause checks only)
- Test: `backend/tests/test_tc_monitor_only.py` (updated fakes) + standalone TC tests

**Interfaces:**
- `strategy_class.run_live`: `ready = get_gateway().state is ConnState.READY` → `ready = broker_ready()` where `from app.broker.status import is_ready_cached as broker_ready` (module-level import so tests can patch `sc_ns["broker_ready"]`). Monitor-only semantics unchanged (market open + not ready → valuation-only).
- `options/monitor.py`: `get_gateway().state is ConnState.READY` → `broker_ready()` (same import pattern).
- `worker.py`: wherever it pauses on gateway state / `session_manager.is_authenticated()`, swap to `broker_ready()`; grep `worker.py` for `gateway|ConnState|is_authenticated` and convert each (report the list).
- Update `tests/test_tc_monitor_only.py`: replace the fake `get_gateway`/`_GW` with `sc_ns["broker_ready"] = lambda: False`; assertions unchanged.

- [ ] **Steps:** apply swaps → run `test_tc_monitor_only` + all 5 standalone TC tests + full suite → commit `feat(strategies): pause conditions read broker probe, not Gateway state`.

---

### Task 11: Facade + providers — IBKR paths removed

**Files:**
- Modify: `backend/app/marketdata/facade.py`, `backend/app/options/chain.py`, `backend/app/providers/__init__.py`
- Delete: `backend/app/providers/ibkr_historical.py`
- Test: update `backend/tests/test_facade.py`, `test_consumers_facade.py`, `test_tt_historical.py`, `test_option_mid_unresolved.py`, `test_builder_chain_quotes.py` as below

**Interfaces:**
- facade: `_race(op, tt_call, ib_call=None)` — keep the parameter (callers shrink over time) but `ib_call` is ignored/None everywhere; delete `_ib_ready`, `_ib_stock_quotes`, and every `ib()` closure; ops become TT → None. `data_status()` unchanged. `chain_expiries`'s IBKR fallback and `option_mid`'s IBKR branch deleted.
- `options/chain.py`: `_ib_option_chain` deleted; `get_option_chain` = facade-only (empty lists when TT down).
- providers: factory returns `TastytradeHistoricalProvider()` unconditionally; `tt_configured` check removed.
- Tests: `test_facade.py` — `test_fallback_to_ibkr_when_ready` becomes `test_tt_miss_returns_none`; drop `_ib_ready` monkeypatches. `test_option_mid_unresolved.py` — repoint to facade-level None behavior. `test_tt_historical.py` — factory test drops the tt_configured branch. Do NOT weaken any sanity-guard or transition-journaling test.

- [ ] **Steps:** TDD the removals (update tests first, watch them fail, apply) → full suite green → commit `feat(marketdata): Tastytrade-only facade; IBKR data paths removed`.

---

### Task 12: The deletion sweep

**Files:**
- Delete: `backend/app/ibkr/` (entire), `backend/app/engine/ibkr_orders.py`
- Modify: `backend/app/config.py` (remove `ibkr_*` settings and `data_primary`), `backend/requirements.txt` (remove `ib_async==2.1.0`), `backend/app/auth/routes.py`/`backend/app/routers/system.py` (any residual imports), `README.md` (Gateway sections → Tastytrade)
- Test: grep gates + full suite

**Steps:**
- [ ] Delete the files/dirs; remove `ibkr_*` + `data_primary` from `Settings` (and the `_tt_state()` `data_primary` key + its frontend read — coordinate with Task 13); remove `ib_async` from requirements.
- [ ] **Grep gates (all must return NOTHING):**
```bash
grep -rn "ib_async\|app\.ibkr\|get_gateway\|ConnState\|ibkr_" backend/app --include="*.py" | grep -v __pycache__
grep -rn "app\.broker\|submit_combo_order\|submit_equity_order" backend/app/options backend/app/routers/builder.py --include="*.py"
```
(The second gate proves the Builder still cannot transmit.)
- [ ] Full suite + all standalone TC tests + `import app.main` — green.
- [ ] Commit: `feat!: remove IBKR entirely — Tastytrade is the sole broker and data source`

---

### Task 13: Frontend — broker card, chips, banners

**Files:**
- Modify: `frontend/src/components/ApiTab.jsx` (IBKR card → Tastytrade broker card: masked account number, net-liq, probe badge, Test connection; remove Connect/Gateway copy), `frontend/src/components/DataBrokerChips.jsx` (Broker chip from `tt.probe === "ok"`, not `auth.conn_state`), `frontend/src/components/ConnectionBanner.jsx` (shows when probe rejected: "Tastytrade credentials rejected — check the API tab"), `frontend/src/api.js` (drop `connect`/`disconnect` if the endpoints are gone — check Task 8 kept `/connect` as force-probe; keep it wired to the Test button)
- Test: build + existing UI behavior manually in Task 14

**Steps:**
- [ ] Apply the three component changes; ApiTab's broker card fetches `api.account()` for net-liq (existing endpoint now TT-backed).
- [ ] `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → `✓ built in …`
- [ ] Commit: `feat(ui): Tastytrade broker card + probe-driven chips/banners`

---

### Task 14: Full verification + deploy + safe live smoke

**Files:** none (verification; commit only fixes).

- [ ] **Step 1:** Full backend suite + 6 standalone TC tests + payoffStats node test + `import app.main` — all green. Re-run the Task 12 grep gates.
- [ ] **Step 2:** `cd frontend && VITE_API_BASE="" npm run build` → deploy (`launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web`) → health OK.
- [ ] **Step 3: Safe live smoke (read-only + dry-run only; ENABLE_LIVE_ORDERS must be False — verify first via config):**
```bash
curl -s http://127.0.0.1:8765/api/auth/tastytrade | python3 -m json.tool     # probe ok
curl -s http://127.0.0.1:8765/api/account | python3 -m json.tool             # real balances
curl -s http://127.0.0.1:8765/api/positions | python3 -m json.tool           # real positions
curl -s http://127.0.0.1:8765/api/strategies/strategy_2/options-position | python3 -c "import sys,json;d=json.load(sys.stdin);print('TC tickers:',[(t['ticker'],t['has_position']) for t in d['tickers']])"
```
Then the dry-run proof from inside the venv (places NOTHING — `dry_run=True`):
```bash
cd backend && ../.venv/bin/python - <<'PY'
import asyncio
from decimal import Decimal
async def main():
    from app.tastytrade.session import get_tt_session
    from app.broker.account import get_tt_account
    from tastytrade.instruments import Equity
    from tastytrade.order import NewOrder, OrderTimeInForce, OrderType, OrderAction
    s = await get_tt_session(); a = await get_tt_account()
    eq = await Equity.get(s, "SPY")
    order = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET,
                     legs=[eq.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN)])
    resp = await a.place_order(s, order, dry_run=True)
    print("dry-run OK — buying power effect:", resp.buying_power_effect)
asyncio.run(main())
PY
```
Expected: real balances/positions render; dry-run returns a buying-power effect (proof the whole order pipeline works) with no order placed (verify: `get_live_orders` empty of SPY).
- [ ] **Step 4:** Kill switch smoke in paper mode: `curl -s -X POST http://127.0.0.1:8765/api/kill` → expect honest not-flat report if TC holds positions (paper mode raises by design) — confirm the response shape and that NO TT order was placed.
- [ ] **Step 5:** Commit any fixes: `git add -A && git commit -m "test(broker): end-to-end verification fixes" || echo clean`

---

## Self-review notes

- **Spec coverage:** account/balances/positions (T1); gated equity orders + dry-run-first (T2); OTOCO brackets + poll/cancel (T3); combos w/ OCC + TO_OPEN/TO_CLOSE + signed price (T4); kill switch (T5); strangler seam (T6); reconciliation/recovery/SAFE_MODE gateway-free (T7); probe-backed auth/status (T8); dashboard/WS/stream (T9); strategy pause swaps incl. TC monitor-only (T10); TT-only facade/providers (T11); total deletion + grep gates incl. Builder no-transmit gate (T12); frontend (T13); verification + safe dry-run live smoke (T14). All five spec safety invariants have named tests (T2 bomb-fakes, T12 builder grep, T5 both modes, T2 no-mid-no-fill, T2/T5 safe-mode blocks).
- **Type consistency:** `NormalizedPosition` moves to `app.broker.account` with identical fields; snapshot dataclasses unchanged; execution's six signatures byte-identical; `probe()/is_ready_cached()` names used consistently in T8/T10/T13.
- **Placeholder scan:** T9–T13 carry directives with exact targets rather than full file bodies where the change is a mechanical import/condition swap on files the implementer must read anyway; every non-mechanical body (broker modules, recovery, status, market-stream test) is complete code. No TBDs.
- **Known risks for implementers:** SDK price-sign convention (verify in T2 and report); `NewComplexOrder` OTOCO field validation happens server-side (dry-run catches it in T14's smoke); `get_positions(include_marks=)` kwarg availability guarded with TypeError fallback (T1); worker.py has 660 lines — T10 touches ONLY pause conditions, listed in the report.
- **Deliberate scope note:** recovery reconciles equities (TradeRecord) exactly as today; TC option-record reconciliation stays with its store (unchanged behavior, spec-conformant).
