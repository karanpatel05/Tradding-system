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
