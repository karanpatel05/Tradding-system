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
    # Fully VALIDATED construction — pydantic validation on outgoing orders is a
    # safety layer and is never bypassed. (Tests therefore fake legs with real
    # tastytrade Leg objects, not SimpleNamespace.)
    return NewOrder(
        time_in_force=OrderTimeInForce.DAY,
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
