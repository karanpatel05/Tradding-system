"""Order execution + position flattening.

Both order entry (``submit_equity_order``) and the kill-switch flatten path
(``flatten_all``) transmit **real broker orders** when ``ENABLE_LIVE_ORDERS`` is
True, and otherwise run in paper mode (compute + log, never transmit). The single
``ENABLE_LIVE_ORDERS`` flag is the one safety switch for real money; pair it with
``TASTYTRADE_IS_TEST=true`` to route to the certification (sandbox) environment
first.

``flatten_all`` is wired into the kill switch. It is **honest about exposure**: if
open positions exist and were not actually closed (paper mode, an unsupported
instrument, or a transmission error), it raises so the kill report records
``fully_flat=False`` rather than silently claiming zero exposure.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.auth.manager import session_manager
from app.config import get_settings
from app.tastytrade.client import get_client

logger = logging.getLogger("trading.execution")

# Maps the strategy's intent to a Tastytrade equity order action.
_ORDER_ACTIONS = {
    "enter_long": "Buy to Open",
    "exit_long": "Sell to Close",
    "enter_short": "Sell to Open",
    "exit_short": "Buy to Close",
}

# Closing action for an existing position, keyed by its direction.
_CLOSE_ACTIONS = {
    "Long": "Sell to Close",
    "Short": "Buy to Close",
}


async def _place_equity_order(session, account, symbol: str, action: str, quantity: int):
    """Build and transmit a market equity order. Kept separate so both order entry
    and flatten share one transmission path (and tests can stub it)."""
    from tastytrade.instruments import Equity
    from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType

    equity = await Equity.get(session, symbol)
    leg = equity.build_leg(Decimal(quantity), OrderAction(action))
    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.MARKET,
        legs=[leg],
    )
    return await account.place_order(session, order, dry_run=False)


async def submit_equity_order(symbol: str, intent: str, quantity: int, *, reason: str = "") -> dict:
    """Place a market equity order for ``quantity`` whole shares.

    SAFETY GATE: when ``ENABLE_LIVE_ORDERS`` is False (the default) this never
    touches the broker — it logs the intended order and returns ``transmitted:
    False`` so the strategy still records the (paper) trade. Set the flag True to
    transmit for real; combine with ``TASTYTRADE_IS_TEST=true`` to route to the
    Tastytrade certification (sandbox) environment first.
    """
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    action = _ORDER_ACTIONS.get(intent)
    if action is None:
        raise ValueError(f"Unknown order intent: {intent}")

    settings = get_settings()
    if not settings.enable_live_orders:
        logger.warning("[PAPER] %s %s x%d (%s) — ENABLE_LIVE_ORDERS off, not transmitted",
                       action, symbol, quantity, reason)
        return {"transmitted": False, "paper": True,
                "detail": f"paper {action} {symbol} x{quantity}"}

    # Real transmission path.
    client = get_client()
    session = await client._fresh_session()
    accounts = await client.get_accounts()
    if not accounts:
        raise RuntimeError("No account available to place order.")
    account = accounts[0]

    placed = await _place_equity_order(session, account, symbol, action, quantity)
    logger.warning("[LIVE ORDER] %s %s x%d on %s (%s)",
                   action, symbol, quantity, account.account_number, reason)
    return {
        "transmitted": True,
        "account": account.account_number,
        "detail": f"LIVE {action} {symbol} x{quantity}",
        "order_id": getattr(getattr(placed, "order", None), "id", None),
    }


def _build_otoco(equity, side: str, quantity: int, stop: float, target: float):
    """Build an OTOCO complex order: a market entry that, once filled, releases an
    OCO pair (stop + target) that closes the position — a resting bracket.

    Pure (takes an already-resolved ``equity`` instrument) so it's unit-testable
    offline. Price sign follows the SDK convention (``get_sign``): a Sell-to-Close
    (long exit) is a CREDIT (+); a Buy-to-Close (short exit) is a DEBIT (−)."""
    from tastytrade.order import (
        ComplexOrderType, NewComplexOrder, NewOrder, OrderAction, OrderTimeInForce, OrderType,
    )

    q = Decimal(str(quantity))
    if side == "long":
        entry_action, close_action = "Buy to Open", "Sell to Close"
        target_price = Decimal(str(target))        # credit
    elif side == "short":
        entry_action, close_action = "Sell to Open", "Buy to Close"
        target_price = -Decimal(str(target))       # debit
    else:
        raise ValueError(f"Unknown side: {side!r}")

    entry = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET,
                     legs=[equity.build_leg(q, OrderAction(entry_action))])
    stop_order = NewOrder(time_in_force=OrderTimeInForce.GTC, order_type=OrderType("Stop"),
                          stop_trigger=Decimal(str(stop)),
                          legs=[equity.build_leg(q, OrderAction(close_action))])
    target_order = NewOrder(time_in_force=OrderTimeInForce.GTC, order_type=OrderType("Limit"),
                            price=target_price,
                            legs=[equity.build_leg(q, OrderAction(close_action))])
    return NewComplexOrder(type=ComplexOrderType.OTOCO, trigger_order=entry,
                           orders=[stop_order, target_order])


async def submit_bracket_order(symbol: str, side: str, quantity: int, entry_price: float,
                               stop: float, target: float, *, reason: str = "") -> dict:
    """Place a **bracket (OTOCO)**: market entry + resting OCO stop/target that the
    broker fills intrabar. This is the live counterpart of the backtest's 1-minute
    fill resolution (Option A).

    SAFETY GATE: identical to ``submit_equity_order`` — when ``ENABLE_LIVE_ORDERS``
    is False (default) nothing is transmitted; it logs and returns
    ``transmitted: False`` so the worker still records the (paper) trade and
    simulates the bracket on 1m bars. Validate the real path in the Tastytrade
    certification env (``TASTYTRADE_IS_TEST=true``) before enabling on real money."""
    if quantity <= 0:
        return {"transmitted": False, "detail": f"skipped: quantity {quantity}"}
    if side not in ("long", "short"):
        raise ValueError(f"Unknown side: {side!r}")

    settings = get_settings()
    if not settings.enable_live_orders:
        logger.warning("[PAPER] BRACKET %s %s x%d entry~%.2f stop %.2f target %.2f (%s) — "
                       "ENABLE_LIVE_ORDERS off, not transmitted",
                       side, symbol, quantity, entry_price, stop, target, reason)
        return {"transmitted": False, "paper": True, "bracket": True,
                "detail": f"paper bracket {side} {symbol} x{quantity} "
                          f"(stop {stop:.2f} / target {target:.2f})",
                "stop": stop, "target": target}

    client = get_client()
    session = await client._fresh_session()
    accounts = await client.get_accounts()
    if not accounts:
        raise RuntimeError("No account available to place order.")
    account = accounts[0]

    from tastytrade.instruments import Equity
    equity = await Equity.get(session, symbol)
    otoco = _build_otoco(equity, side, quantity, stop, target)
    placed = await account.place_complex_order(session, otoco, dry_run=False)
    logger.warning("[LIVE BRACKET] %s %s x%d stop %.2f target %.2f on %s (%s)",
                   side, symbol, quantity, stop, target, account.account_number, reason)
    cx = getattr(placed, "complex_order", None) or placed
    return {
        "transmitted": True, "bracket": True, "account": account.account_number,
        "detail": f"LIVE bracket {side} {symbol} x{quantity}",
        "complex_order_id": getattr(cx, "id", None),
        "stop": stop, "target": target,
    }


async def cancel_symbol_orders(symbol: str) -> dict:
    """Cancel any resting (live) orders for ``symbol`` — used by the worker before a
    crossover/EOD market-close so the OCO bracket doesn't linger. Paper: no-op."""
    settings = get_settings()
    if not settings.enable_live_orders:
        return {"cancelled": 0, "paper": True}
    client = get_client()
    session = await client._fresh_session()
    cancelled = 0
    for account in await client.get_accounts():
        cancelled += await _cancel_open_orders(session, account, symbol=symbol)
    return {"cancelled": cancelled}


def _order_symbols(order) -> set[str]:
    """Underlying symbols touched by a (complex or simple) order, best-effort."""
    out: set[str] = set()
    legs = list(getattr(order, "legs", None) or [])
    for sub in getattr(order, "orders", None) or []:
        legs += list(getattr(sub, "legs", None) or [])
    trig = getattr(order, "trigger_order", None)
    if trig is not None:
        legs += list(getattr(trig, "legs", None) or [])
    for leg in legs:
        sym = getattr(leg, "symbol", None)
        if sym:
            out.add(str(sym))
    return out


async def _cancel_open_orders(session, account, symbol: str | None = None) -> int:
    """Cancel live simple + complex orders on an account (optionally only those
    touching ``symbol``). Returns the count cancelled. Best-effort per order."""
    cancelled = 0
    try:
        complex_orders = await account.get_live_complex_orders(session)
    except Exception:  # noqa: BLE001
        complex_orders = []
    for co in complex_orders or []:
        if symbol and symbol not in _order_symbols(co):
            continue
        try:
            await account.delete_complex_order(session, co.id)
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to cancel complex order %s: %r", getattr(co, "id", "?"), exc)
    try:
        orders = await account.get_live_orders(session)
    except Exception:  # noqa: BLE001
        orders = []
    for o in orders or []:
        if symbol and symbol not in _order_symbols(o):
            continue
        try:
            await account.delete_order(session, o.id)
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to cancel order %s: %r", getattr(o, "id", "?"), exc)
    return cancelled


async def poll_bracket_fill(symbol: str, complex_order_id: str | None) -> dict | None:
    """Best-effort: has a symbol's resting bracket (OTOCO) filled at the broker?

    Returns ``None`` while the position is still open; otherwise
    ``{"price", "ms", "reason"}`` for the filled stop/target child. Paper mode (and a
    missing id) → ``None`` (the worker simulates fills on 1m instead). Real-money use
    must be validated in the Tastytrade certification env first."""
    settings = get_settings()
    if not settings.enable_live_orders or not complex_order_id:
        return None
    client = get_client()
    session = await client._fresh_session()
    accounts = await client.get_accounts()

    # Still holding it at the broker → bracket hasn't filled.
    for account in accounts:
        for pos in await client.get_positions(account):
            if str(getattr(pos, "symbol", "")) == symbol and abs(int(getattr(pos, "quantity", 0) or 0)) > 0:
                return None

    # Flat at the broker → the OCO filled. Pull the filled child for price/reason.
    for account in accounts:
        try:
            co = await account.get_complex_order(session, complex_order_id)
        except Exception:  # noqa: BLE001
            continue
        for child in getattr(co, "orders", None) or []:
            if str(getattr(getattr(child, "status", ""), "value", getattr(child, "status", ""))) != "Filled":
                continue
            otype = str(getattr(getattr(child, "order_type", ""), "value", getattr(child, "order_type", "")))
            if otype == "Stop":
                price = float(getattr(child, "stop_trigger", 0) or 0)
                return {"price": price, "ms": None, "reason": "stop"}
            price = abs(float(getattr(child, "price", 0) or 0))
            return {"price": price, "ms": None, "reason": "target"}
    # Filled but couldn't read the child — report it closed without a precise price.
    return {"price": None, "ms": None, "reason": "bracket"}


async def flatten_all() -> str:
    """Cancel every resting order, then close every open equity position. Returns a
    human-readable detail string.

    Transmits real closing orders when ``ENABLE_LIVE_ORDERS`` is True; otherwise
    runs in paper mode (logs what it *would* send, transmits nothing).

    Raises on anything that leaves real exposure standing — paper mode with open
    positions, a non-equity instrument we can't auto-close, or a transmission
    failure — so the kill switch reports flatten as not-OK (``fully_flat=False``)
    rather than silently claiming zero exposure. Best-effort: every position is
    attempted before the error is raised.
    """
    if not session_manager.is_authenticated():
        return "No active session — nothing to flatten."

    settings = get_settings()
    client = get_client()
    accounts = await client.get_accounts()

    total = 0
    closed: list[str] = []
    failures: list[str] = []
    session = None  # fetched lazily, only when we actually need to transmit

    # Cancel resting orders FIRST so a bracket's stop/target can't fire mid-kill.
    # Paper mode has nothing resting (orders are never transmitted), so skip.
    cancelled_orders = 0
    if settings.enable_live_orders:
        session = await client._fresh_session()
        for account in accounts:
            try:
                cancelled_orders += await _cancel_open_orders(session, account)
            except Exception as exc:  # noqa: BLE001 — record, don't abort the kill
                failures.append(f"order-cancel ({getattr(account, 'account_number', '?')}): {exc}")

    for account in accounts:
        positions = await client.get_positions(account)
        for pos in positions:
            total += 1
            sym = getattr(pos, "symbol", "?")
            direction = getattr(pos, "quantity_direction", None)
            instrument = getattr(pos, "instrument_type", None)
            instrument_val = getattr(instrument, "value", instrument)

            if str(instrument_val) != "Equity":
                failures.append(f"{sym} ({instrument_val}: not an equity, cannot auto-flatten)")
                continue

            action = _CLOSE_ACTIONS.get(direction)
            if action is None:
                failures.append(f"{sym} (unknown direction {direction!r})")
                continue

            try:
                qty = abs(int(getattr(pos, "quantity", 0)))
            except (TypeError, ValueError):
                failures.append(f"{sym} (bad quantity {getattr(pos, 'quantity', None)!r})")
                continue
            if qty <= 0:
                total -= 1  # zero-quantity ledger entry, not real exposure
                continue

            if not settings.enable_live_orders:
                logger.warning("[FLATTEN-PAPER] would %s %s x%d on %s — ENABLE_LIVE_ORDERS off",
                               action, sym, qty, account.account_number)
                failures.append(f"{sym} x{qty} (paper: not transmitted)")
                continue

            try:
                if session is None:
                    session = await client._fresh_session()
                await _place_equity_order(session, account, sym, action, qty)
                logger.warning("[FLATTEN-LIVE] %s %s x%d on %s",
                               action, sym, qty, account.account_number)
                closed.append(f"{sym} x{qty}")
            except Exception as exc:  # noqa: BLE001 — must attempt every position
                logger.error("Flatten failed for %s: %r", sym, exc)
                failures.append(f"{sym} ({exc})")

    orders_note = f"cancelled {cancelled_orders} resting order(s); " if cancelled_orders else ""
    if total == 0 and not failures:
        return f"{orders_note}No open positions." if orders_note else "No open positions."

    if failures:
        raise RuntimeError(
            f"Flatten incomplete: {orders_note}closed {len(closed)}/{total} "
            f"[{', '.join(closed) or 'none'}]; not flat: {', '.join(failures)}"
        )
    return f"{orders_note}Flattened {total} position(s): {', '.join(closed)}"