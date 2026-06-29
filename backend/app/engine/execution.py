"""Order execution + flatten over IBKR (ib_async).

ENABLE_LIVE_ORDERS gates transmission: off = log + return transmitted:False (the
worker still records the paper trade); on = transmit to the IBKR paper account.
``flatten_all`` stays honest: it raises if any real exposure remains, so the kill
switch reports ``fully_flat=False`` rather than silently claiming zero exposure.

Public API is unchanged from the previous Tastytrade implementation so callers
(strategy worker, kill switch, routers) need no changes:
``submit_equity_order``, ``submit_bracket_order``, ``cancel_symbol_orders``,
``poll_bracket_fill``, ``flatten_all``."""
from __future__ import annotations

import logging

from ib_async import MarketOrder

from app.config import get_settings
from app.engine.ibkr_orders import build_bracket, intent_to_action
from app.ibkr.account import get_positions
from app.ibkr.contracts import stock
from app.ibkr.gateway import get_gateway

logger = logging.getLogger("trading.execution")


async def submit_equity_order(symbol: str, intent: str, quantity: int, *, reason: str = "") -> dict:
    """Place a market equity order for ``quantity`` whole shares.

    SAFETY GATE: when ``ENABLE_LIVE_ORDERS`` is False (default) this never touches
    the broker — it logs and returns ``transmitted: False`` so the strategy still
    records the (paper) trade."""
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    action = intent_to_action(intent)
    from app.engine.safe_mode import is_safe_mode
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({symbol})"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] %s %s x%d (%s) — ENABLE_LIVE_ORDERS off, not transmitted",
                       action, symbol, quantity, reason)
        return {"transmitted": False, "paper": True,
                "detail": f"paper {action} {symbol} x{quantity}"}
    from app.engine.order_store import mark_perm_id, record_intent
    ref = await record_intent("", symbol, intent, quantity)  # durable BEFORE transmit
    ib = await get_gateway().get_ib()
    contract = stock(symbol)
    await ib.qualifyContractsAsync(contract)
    order = MarketOrder(action, quantity)
    order.orderRef = ref
    trade = ib.placeOrder(contract, order)
    perm = getattr(getattr(trade, "order", None), "permId", None)
    if perm:
        await mark_perm_id(ref, str(perm))
    logger.warning("[LIVE] %s %s x%d (%s) ref=%s", action, symbol, quantity, reason, ref)
    return {"transmitted": True, "ref": ref,
            "detail": f"LIVE {action} {symbol} x{quantity}",
            "order_id": getattr(trade.order, "orderId", None)}


async def submit_bracket_order(symbol: str, side: str, quantity: int, entry_price: float,
                               stop: float, target: float, *, reason: str = "") -> dict:
    """Place a native IBKR bracket: market entry + OCO stop/target the broker fills
    intrabar. SAFETY GATE identical to ``submit_equity_order``."""
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    if side not in ("long", "short"):
        raise ValueError(f"Unknown side: {side!r}")
    from app.engine.safe_mode import is_safe_mode
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({symbol})"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] BRACKET %s %s x%d entry~%.2f stop %.2f target %.2f (%s) — "
                       "ENABLE_LIVE_ORDERS off, not transmitted",
                       side, symbol, quantity, entry_price, stop, target, reason)
        return {"transmitted": False, "paper": True, "bracket": True,
                "detail": f"paper bracket {side} {symbol} x{quantity} "
                          f"(stop {stop:.2f} / target {target:.2f})",
                "stop": stop, "target": target}
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
    return {"transmitted": True, "bracket": True,
            "detail": f"LIVE bracket {side} {symbol} x{quantity}",
            "parent_order_id": parent_id, "stop": stop, "target": target}


async def cancel_symbol_orders(symbol: str) -> dict:
    """Cancel any resting (live) orders for ``symbol``. Paper: no-op."""
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
    """Best-effort: has a symbol's resting bracket filled at the broker?

    None while the position is still open; otherwise ``{price, ms, reason}`` for the
    filled stop/target child. Paper mode → None (the worker simulates fills on 1m).
    ``ref`` (the old complex_order_id) is accepted for call-site compatibility."""
    s = get_settings()
    if not s.enable_live_orders:
        return None
    ib = await get_gateway().get_ib()
    for p in await get_positions():
        if p.symbol == symbol and p.quantity != 0:
            return None  # still holding it
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
    """Cancel every resting order, then market-close every open equity position.

    Transmits real closing orders when ``ENABLE_LIVE_ORDERS`` is True; otherwise
    paper mode (logs, transmits nothing). Raises on anything that leaves real
    exposure standing so the kill switch reports flatten as not-OK."""
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
                ib.cancelOrder(trade.order)
                cancelled += 1
            except Exception as exc:  # noqa: BLE001 — record, don't abort the kill
                failures.append(f"order-cancel {getattr(trade.contract, 'symbol', '?')}: {exc}")

    total = len([p for p in positions if abs(p.quantity) > 0])
    for p in positions:
        qty = abs(p.quantity)
        if qty <= 0:
            continue
        if p.instrument_type != "Equity":
            continue  # options/combos closed in the raw-contract loop below
        action = "SELL" if p.quantity > 0 else "BUY"
        if not s.enable_live_orders:
            logger.warning("[FLATTEN-PAPER] would %s %s x%d — ENABLE_LIVE_ORDERS off",
                           action, p.symbol, qty)
            failures.append(f"{p.symbol} x{qty} (paper: not transmitted)")
            continue
        try:
            c = stock(p.symbol)
            await ib.qualifyContractsAsync(c)
            ib.placeOrder(c, MarketOrder(action, qty))
            logger.warning("[FLATTEN-LIVE] %s %s x%d", action, p.symbol, qty)
            closed.append(f"{p.symbol} x{qty}")
        except Exception as exc:  # noqa: BLE001 — must attempt every position
            logger.error("Flatten failed for %s: %r", p.symbol, exc)
            failures.append(f"{p.symbol} ({exc})")

    # Close non-equity (option/combo) positions via their raw IBKR contracts (the
    # normalized view can't rebuild an option contract; the raw position has its conId).
    for rp in ib.positions():
        if getattr(rp.contract, "secType", "STK") == "STK" or not rp.position:
            continue
        qy = abs(int(rp.position))
        act = "SELL" if rp.position > 0 else "BUY"
        label = rp.contract.localSymbol or rp.contract.symbol
        if not s.enable_live_orders:
            logger.warning("[FLATTEN-PAPER] would %s option %s x%d — ENABLE_LIVE_ORDERS off",
                           act, label, qy)
            failures.append(f"{label} (paper: not transmitted)")
            continue
        try:
            await ib.qualifyContractsAsync(rp.contract)
            ib.placeOrder(rp.contract, MarketOrder(act, qy))
            logger.warning("[FLATTEN-LIVE] %s option %s x%d", act, label, qy)
            closed.append(f"{label} x{qy}")
        except Exception as exc:  # noqa: BLE001 — attempt every position
            logger.error("Flatten failed for option %s: %r", label, exc)
            failures.append(f"{label} option ({exc})")

    note = f"cancelled {cancelled} resting order(s); " if cancelled else ""
    if total == 0 and not failures:
        return f"{note}No open positions." if note else "No open positions."
    if failures:
        raise RuntimeError(f"Flatten incomplete: {note}closed {len(closed)}/{total} "
                           f"[{', '.join(closed) or 'none'}]; not flat: {', '.join(failures)}")
    return f"{note}Flattened {total} position(s): {', '.join(closed)}"


async def submit_combo_order(underlying: str, legs: list[dict], *, net_price: float | None = None,
                             action: str = "BUY", reason: str = "") -> dict:
    """Place a multi-leg options combo (BAG). Each leg = {expiry(date), strike, right, action}.
    GATE: ENABLE_LIVE_ORDERS off -> log + return transmitted:False (paper)."""
    if not legs:
        return {"transmitted": False, "detail": "no legs"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] COMBO %s %s x%d legs net~%s (%s) — ENABLE_LIVE_ORDERS off",
                       action, underlying, len(legs), net_price, reason)
        return {"transmitted": False, "paper": True, "legs": len(legs),
                "detail": f"paper combo {action} {underlying} {len(legs)} legs"}
    from ib_async import LimitOrder, MarketOrder

    from app.ibkr.contracts import combo, option
    ib = await get_gateway().get_ib()
    leg_ids: list[tuple[int, str]] = []
    for spec in legs:
        c = option(underlying, spec["expiry"], spec["strike"], spec["right"])
        await ib.qualifyContractsAsync(c)
        leg_ids.append((c.conId, spec["action"]))
    bag = combo(underlying, leg_ids)
    order = LimitOrder(action, 1, net_price) if net_price is not None else MarketOrder(action, 1)
    trade = ib.placeOrder(bag, order)
    logger.warning("[LIVE COMBO] %s %s %d legs net %s (%s)", action, underlying, len(legs),
                   net_price, reason)
    return {"transmitted": True, "legs": len(legs), "order_id": trade.order.orderId,
            "detail": f"LIVE combo {action} {underlying} {len(legs)} legs"}
