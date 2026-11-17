"""Order routing to the Alpaca PAPER account, gated by ENABLE_LIVE_ORDERS (default OFF).

The paper account is SHARED, so the app only ever submits, cancels and counts orders it
created: every submit carries `client_order_id = tag(ref)` (`tsys-` + order-store ref).

OFF -> no network order call; a simulated fill at the sanity-guarded facade mid is returned
       (transmitted: False, paper: True). A missing mid means NO fill, never a made-up price.
ON  -> check order on every transmit path: quantity -> SAFE_MODE -> gate -> paper lock
       (`trading_base()`; a non-paper host is refused before any intent row or request) ->
       durable `record_intent` -> POST /v2/orders (tagged) -> `mark_perm_id(ref, alpaca_id)`.
       Alpaca has no dry-run; a 4xx rejection returns transmitted: False with its message.

Return shapes match the old app/engine/execution.py so callers don't change.

`flatten_all` (and the Tastytrade helpers below it) is still the Tastytrade kill-switch
path; it is rewritten for Alpaca separately and is intentionally untouched here."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from decimal import Decimal

import httpx

from app.broker import alpaca_client as ac
from app.broker.alpaca_client import AlpacaError, PaperLockError
from app.config import get_settings
from app.engine.safe_mode import is_safe_mode  # noqa: F401 — monkeypatch point

logger = logging.getLogger("trading.broker.orders")

BLOCKED_DETAIL = "blocked: Alpaca live endpoint locked (paper only)"
MAX_COMBO_LEGS = 4          # Alpaca mleg limit; never split a combo
_CANCEL_WAIT = 5.0          # seconds to wait for cancelled orders to leave the book
_CANCEL_STEP = 0.25

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
    # safety layer and is never bypassed.
    return NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT if order_type == "LIMIT" else OrderType.MARKET,
        legs=legs, price=price)


# --- Alpaca transmit plumbing -------------------------------------------------
def _paper_lock_blocked() -> dict | None:
    """The blocked dict when the configured Alpaca host is not the paper host."""
    try:
        ac.trading_base()
    except PaperLockError:
        logger.error("Order refused: Alpaca base URL is not %s (paper lock)", ac.PAPER_HOST)
        return {"transmitted": False, "detail": BLOCKED_DETAIL}
    return None


def _px(v: float) -> str:
    """Alpaca rejects sub-penny prices; strategy stop/target levels are unrounded floats."""
    return str(round(float(v), 2))


async def _transmit(body: dict, ref: str, what: str) -> dict:
    """POST a tagged order. Rejections and transport errors become transmitted: False
    (never paper) so the worker stays flat; the intent row keeps the ref for recon."""
    from app.engine.order_store import mark_perm_id
    body = {**body, "client_order_id": ac.tag(ref)}
    try:
        placed = await ac.request("POST", "/v2/orders", json=body, write=True)
    except PaperLockError:       # base URL changed between the check and the POST
        logger.error("%s refused by paper lock (ref %s)", what, ref)
        return {"transmitted": False, "detail": BLOCKED_DETAIL, "ref": ref}
    except AlpacaError as exc:
        logger.error("%s rejected by Alpaca (ref %s): %s", what, ref, exc.message)
        return {"transmitted": False, "detail": f"rejected by Alpaca: {exc.message}",
                "ref": ref}
    except httpx.HTTPError as exc:
        logger.error("%s: Alpaca request failed (ref %s): %r", what, ref, exc)
        return {"transmitted": False, "ref": ref,
                "detail": f"Alpaca request failed ({type(exc).__name__}); "
                          f"order state unknown, ref {ref}"}
    oid = str((placed or {}).get("id") or "")
    if oid:
        await mark_perm_id(ref, oid)
    return {"transmitted": True, "order_id": oid, "ref": ref}


# --- public order API ---------------------------------------------------------
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
    if (blocked := _paper_lock_blocked()) is not None:
        return blocked
    from app.engine.order_store import record_intent
    ref = await record_intent("", symbol, intent, quantity)   # durable BEFORE transmit
    out = await _transmit({"symbol": symbol, "qty": str(int(quantity)),
                           "side": action.lower(), "type": "market",
                           "time_in_force": "day"}, ref, f"{action} {symbol}")
    out.setdefault("detail", f"LIVE {action} {symbol} x{quantity}")
    logger.warning("[LIVE] %s %s x%d (%s) -> %s", action, symbol, quantity, reason, out["detail"])
    return out


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
    if (blocked := _paper_lock_blocked()) is not None:
        return blocked
    from app.engine.order_store import record_intent
    intent = "enter_long" if side == "long" else "enter_short"
    ref = await record_intent("", symbol, intent, quantity)
    out = await _transmit({"symbol": symbol, "qty": str(int(quantity)),
                           "side": "buy" if side == "long" else "sell", "type": "market",
                           "time_in_force": "gtc", "order_class": "bracket",
                           "take_profit": {"limit_price": _px(target)},
                           "stop_loss": {"stop_price": _px(stop)}},
                          ref, f"BRACKET {side} {symbol}")
    if not out["transmitted"]:
        return out
    logger.warning("[LIVE BRACKET] %s %s x%d stop %.2f target %.2f (%s)",
                   side, symbol, quantity, stop, target, reason)
    return {"transmitted": True, "bracket": True, "parent_order_id": out["order_id"],
            "ref": ref, "detail": f"LIVE bracket {side} {symbol} x{quantity}",
            "stop": stop, "target": target}


def _oget(o, key, default=None):
    """Live orders are Alpaca dicts (legacy objects tolerated by flatten_all)."""
    return o.get(key, default) if isinstance(o, dict) else getattr(o, key, default)


_OCC = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")


def _matches(o: dict, symbol: str) -> bool:
    """Order (or any of its legs) is for `symbol`; an underlying also matches its options."""
    want = ac.occ_unpadded(symbol).upper()
    syms = [o.get("symbol")] + [l.get("symbol") for l in (o.get("legs") or [])]
    for raw in syms:
        sym = ac.occ_unpadded(raw or "").upper()
        if not sym:
            continue
        if sym == want:
            return True
        m = _OCC.match(sym)
        if m and m.group(1) == want:
            return True
    return False


async def cancel_symbol_orders(symbol: str) -> dict:
    """Cancel app-owned open orders for `symbol` (incl. resting bracket legs), then wait
    until they leave the book — Alpaca reserves shares for open bracket legs, so the exit
    that follows would otherwise reject."""
    s = get_settings()
    if not s.enable_live_orders:
        return {"cancelled": 0, "paper": True}
    if (blocked := _paper_lock_blocked()) is not None:
        return {"cancelled": 0, "detail": blocked["detail"]}
    from app.broker.ledger import app_open_orders
    orders, owned = await app_open_orders()
    cancelled = 0
    for o in orders:
        oid = o.get("id")
        if oid not in owned or not _matches(o, symbol):
            continue
        try:
            await ac.request("DELETE", f"/v2/orders/{oid}", write=True)
            cancelled += 1
        except (AlpacaError, httpx.HTTPError, PaperLockError) as exc:
            # e.g. the OCO sibling of a leg just cancelled is no longer cancelable
            logger.warning("Cancel of app order %s (%s) failed: %r", oid, symbol, exc)
    deadline = time.monotonic() + _CANCEL_WAIT
    while True:
        try:
            still = [o for o in (await app_open_orders())[0] if _matches(o, symbol)]
        except (AlpacaError, httpx.HTTPError) as exc:
            logger.warning("Open-order check after cancel failed for %s: %r", symbol, exc)
            still = [True]
        if not still:
            return {"cancelled": cancelled}
        if time.monotonic() >= deadline:
            logger.warning("App orders for %s still open %.1fs after cancel", symbol,
                           _CANCEL_WAIT)
            return {"cancelled": cancelled, "pending": True}
        await asyncio.sleep(_CANCEL_STEP)


def occ_symbol_for(underlying: str, expiry, strike: float, right: str) -> str:
    """Unpadded OCC option symbol (Alpaca format): root + yymmdd + C/P + strike*1000
    zero-padded to 8 digits, e.g. QQQ260807C00725000."""
    ymd = expiry.strftime("%y%m%d")
    k = f"{int(round(float(strike) * 1000)):08d}"
    return f"{underlying.upper().strip()}{ymd}{right.upper()}{k}"


async def submit_combo_order(underlying: str, legs: list[dict], *, net_price: float | None = None,
                             action: str = "BUY", reason: str = "", quantity: int = 1) -> dict:
    """Multi-leg options order (Alpaca `mleg`). Each leg = {expiry(date), strike, right,
    action}. Per-leg position_intent: (buy|sell) + (_to_close if the APP'S OWN net
    position in that OCC symbol is on the opposite side, else _to_open) — never the shared
    account's positions. Limit at |net_price| if given, else market."""
    if not legs:
        return {"transmitted": False, "detail": "no legs"}
    if len(legs) > MAX_COMBO_LEGS:
        return {"transmitted": False,
                "detail": f"rejected: Alpaca multi-leg orders allow at most "
                          f"{MAX_COMBO_LEGS} legs (got {len(legs)}); not split"}
    if await is_safe_mode():
        return {"transmitted": False, "safe_mode": True,
                "detail": f"blocked: SAFE_MODE ({underlying})"}
    s = get_settings()
    if not s.enable_live_orders:
        logger.warning("[PAPER] COMBO %s %s x%d legs net~%s (%s)",
                       action, underlying, len(legs), net_price, reason)
        return {"transmitted": False, "paper": True, "legs": len(legs),
                "detail": f"paper combo {action} {underlying} {len(legs)} legs"}
    if (blocked := _paper_lock_blocked()) is not None:
        return blocked
    from app.broker.ledger import app_net_positions
    from app.engine.order_store import record_intent
    q = max(1, int(quantity))
    try:
        app_net = await app_net_positions()
    except (AlpacaError, httpx.HTTPError) as exc:
        logger.error("Combo %s: app position lookup failed: %r", underlying, exc)
        return {"transmitted": False, "legs": len(legs),
                "detail": f"app position lookup failed: {exc}"}
    alp_legs = []
    for spec in legs:
        occ = occ_symbol_for(underlying, spec["expiry"], spec["strike"], spec["right"])
        side = "buy" if spec["action"].upper() == "BUY" else "sell"
        held = app_net.get(occ, 0)
        closing = held < 0 if side == "buy" else held > 0
        alp_legs.append({"symbol": occ, "ratio_qty": "1", "side": side,
                         "position_intent": f"{side}_to_{'close' if closing else 'open'}"})
    body: dict = {"order_class": "mleg", "qty": str(q),
                  "type": "limit" if net_price is not None else "market"}
    if net_price is not None:
        body["limit_price"] = str(round(abs(float(net_price)), 2))
    body.update({"time_in_force": "day", "legs": alp_legs})
    ref = await record_intent("", underlying, f"combo_{action.lower()}", q)
    out = await _transmit(body, ref, f"COMBO {action} {underlying}")
    out["legs"] = len(legs)
    out.setdefault("detail", f"LIVE combo {action} {underlying} {len(legs)} legs")
    logger.warning("[LIVE COMBO] %s %s %d legs net %s (%s) -> %s",
                   action, underlying, len(legs), net_price, reason, out["detail"])
    return out


def _epoch_ms(ts: str | None) -> int | None:
    """Alpaca RFC3339 timestamps (up to 9 fractional digits, 'Z') -> epoch ms."""
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        logger.warning("Unparseable Alpaca timestamp %r", ts)
        return None


async def poll_bracket_fill(symbol: str, ref=None) -> dict | None:
    """Decided from the APP'S bracket order (the account is shared, so its position says
    nothing): None while no exit child has filled; else {price, ms, reason} from the filled
    child (stop vs take-profit). `ref` is the stored parent_order_id (Alpaca order id).
    Paper mode -> None (the worker simulates fills on 1m bars, unchanged)."""
    s = get_settings()
    if not s.enable_live_orders:
        return None
    from app.broker.ledger import app_orders
    try:
        orders = await app_orders("all")
    except (AlpacaError, httpx.HTTPError) as exc:  # transient, distinct from "no fill"
        logger.warning("Bracket fill lookup failed for %s: %r", symbol, exc)
        return {"price": None, "ms": None, "reason": "error"}
    want = ac.occ_unpadded(symbol).upper()
    parent = next((o for o in orders if ref and o.get("id") == ref), None)
    if parent is None:   # newest first -> the latest app bracket for the symbol
        parent = next((o for o in orders if o.get("order_class") == "bracket"
                       and ac.occ_unpadded(o.get("symbol") or "").upper() == want), None)
    if parent is None:
        return None
    for child in parent.get("legs") or []:
        if child.get("status") != "filled":
            continue
        kind = {"stop": "stop", "limit": "target"}.get(child.get("type"))
        if kind is None:
            continue
        price = child.get("filled_avg_price")
        return {"price": float(price) if price not in (None, "") else None,
                "ms": _epoch_ms(child.get("filled_at")), "reason": kind}
    return None


# --- Tastytrade kill-switch path (rewritten for Alpaca separately) -------------
async def _option(session, occ_symbol: str):
    from tastytrade.instruments import Option
    return await Option.get(session, occ_symbol)


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
                if await _cancel_order(_oget(o, "id", "")):
                    cancelled += 1
            except Exception as exc:  # noqa: BLE001 — record, keep killing
                failures.append(f"order-cancel {_oget(o, 'id', '?')}: {exc}")

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
