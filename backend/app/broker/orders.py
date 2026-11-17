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

`flatten_all` (kill switch) cancels and closes ONLY app-owned orders/quantities, each
close a tagged order on the same paper-lock -> intent -> transmit path; it never uses
Alpaca's bulk DELETE /v2/orders or /v2/positions."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

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
_FLATTEN_WAIT = 10.0        # seconds the kill switch polls the app ledger for close fills
_FLATTEN_STEP = 0.5
_LOOKUP_ERRORS = (AlpacaError, httpx.HTTPError)
_TERMINAL = {"canceled", "expired", "rejected", "replaced", "done_for_day"}  # order ended

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
async def _mid(symbol: str) -> float | None:
    from app.marketdata.facade import stock_quote
    q = await stock_quote(symbol)
    return (q or {}).get("price")


# --- Alpaca transmit plumbing -------------------------------------------------
def _paper_lock_blocked() -> dict | None:
    """The blocked dict when the configured Alpaca host is not the paper host."""
    try:
        ac.trading_base()
    except PaperLockError:
        logger.error("Order refused: Alpaca base URL is not %s (paper lock)", ac.PAPER_HOST)
        return {"transmitted": False, "detail": BLOCKED_DETAIL}
    return None


def _market_body(symbol: str, qty: int, side: str, position_intent: str | None = None) -> dict:
    """Single-symbol market DAY order body (equity, or a single option leg when
    `position_intent` is given, e.g. sell_to_close)."""
    body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "market",
            "time_in_force": "day"}
    if position_intent:
        body["position_intent"] = position_intent
    return body


def _px(v: float) -> str:
    """Alpaca rejects sub-penny prices; strategy stop/target levels are unrounded floats."""
    return str(round(float(v), 2))


def _is_duplicate_coid(exc: AlpacaError) -> bool:
    msg = (exc.message or "").lower()
    return exc.status in (409, 422) and "client_order_id" in msg and (
        "unique" in msg or "duplicate" in msg or "exist" in msg)


async def _resolve_duplicate(ref: str, what: str) -> dict:
    """A retry reused `ref` and Alpaca already holds an order with that client_order_id:
    the earlier POST landed. Report THAT order instead of sending a second one."""
    from app.engine.order_store import mark_perm_id
    try:
        o = await ac.request("GET", "/v2/orders:by_client_order_id",
                             params={"client_order_id": ac.tag(ref)})
    except (AlpacaError, httpx.HTTPError) as exc:
        logger.error("%s: duplicate client_order_id lookup failed (ref %s): %r", what, ref, exc)
        return {"transmitted": False, "unknown_state": True, "ref": ref,
                "detail": f"duplicate client_order_id, lookup failed; order state unknown, "
                          f"ref {ref}"}
    o = o or {}
    status = o.get("status")
    try:
        filled = float(o.get("filled_qty") or 0)
    except (TypeError, ValueError):
        filled = 0.0
    if status in _TERMINAL and filled == 0:
        logger.error("%s: earlier submit for ref %s ended %s", what, ref, status)
        return {"transmitted": False, "ref": ref,
                "detail": f"earlier submit for ref {ref} ended {status}"}
    oid = str(o.get("id") or "")
    if oid:
        await mark_perm_id(ref, oid)
    logger.warning("%s: ref %s already at Alpaca as %s (%s); not resubmitted",
                   what, ref, oid, status)
    return {"transmitted": True, "order_id": oid, "ref": ref}


async def _transmit(body: dict, ref: str, what: str) -> dict:
    """POST a tagged order. Rejections and transport errors become transmitted: False
    (never paper) so the worker stays flat; the intent row keeps the ref for recon.
    A transport error is flagged `unknown_state` (the POST may have landed): retry with
    the same `ref` so the client_order_id is identical; a duplicate-id rejection is then
    resolved to the existing order."""
    from app.engine.order_store import mark_perm_id
    body = {**body, "client_order_id": ac.tag(ref)}
    try:
        placed = await ac.request("POST", "/v2/orders", json=body, write=True)
    except PaperLockError:       # base URL changed between the check and the POST
        logger.error("%s refused by paper lock (ref %s)", what, ref)
        return {"transmitted": False, "detail": BLOCKED_DETAIL, "ref": ref}
    except AlpacaError as exc:
        if _is_duplicate_coid(exc):
            return await _resolve_duplicate(ref, what)
        logger.error("%s rejected by Alpaca (ref %s): %s", what, ref, exc.message)
        return {"transmitted": False, "detail": f"rejected by Alpaca: {exc.message}",
                "ref": ref}
    except httpx.HTTPError as exc:
        logger.error("%s: Alpaca request failed (ref %s): %r", what, ref, exc)
        return {"transmitted": False, "ref": ref, "unknown_state": True,
                "detail": f"Alpaca request failed ({type(exc).__name__}); "
                          f"order state unknown, ref {ref}"}
    oid = str((placed or {}).get("id") or "")
    if oid:
        await mark_perm_id(ref, oid)
    return {"transmitted": True, "order_id": oid, "ref": ref}


# --- public order API ---------------------------------------------------------
async def submit_equity_order(symbol: str, intent: str, quantity: int, *,
                              reason: str = "", ref: str | None = None) -> dict:
    """`ref`: reuse an existing order-store ref (retry after an unknown-state failure) so
    the client_order_id is identical and Alpaca refuses a duplicate."""
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
    if not ref:
        ref = await record_intent("", symbol, intent, quantity)   # durable BEFORE transmit
    out = await _transmit(_market_body(symbol, quantity, action.lower()), ref,
                          f"{action} {symbol}")
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


async def _lingering(symbol: str | None = None) -> list[dict]:
    """App-owned orders/legs for `symbol` (None = any symbol) still on the book. Unlike
    the ledger's open set this includes `pending_cancel`: Alpaca DELETE is async (204 ->
    pending_cancel -> canceled) and shares stay reserved until the cancel settles."""
    from app.broker.ledger import OPEN_STATUSES, app_orders
    live = OPEN_STATUSES | {"pending_cancel"}
    out: list[dict] = []
    for parent in await app_orders("all"):
        if parent.get("status") in live and (symbol is None or _matches(parent, symbol)):
            out.append(parent)
        if parent.get("order_class") == "mleg":
            continue
        out.extend(l for l in parent.get("legs") or []
                   if l.get("status") in live and (symbol is None or _matches(l, symbol)))
    return out


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
            still = await _lingering(symbol)
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
    account's positions. Limit at the net price if given (signed, see below), else market."""
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
        # Alpaca mleg sign convention: positive limit = debit (BUY), negative = credit
        # (SELL). MUST be verified against Alpaca's docs before any live combo use.
        net = round(abs(float(net_price)), 2)
        body["limit_price"] = str(net if action.upper() == "BUY" else -net)
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
    nothing). `ref` is the stored parent_order_id (Alpaca order id).
    - an exit child filled -> {price, ms, reason: "stop" | "target"}
    - None only while genuinely holding: an exit child is still live (or the entry is
      still working)
    - {price: None, ms: None, reason: "bracket"} for a dead bracket: no app bracket for
      the ref/symbol, the parent ended unfilled (canceled/rejected/expired), or the entry
      filled but every exit child ended without a fill (the worker escalates to SAFE_MODE)
    - {..., reason: "error"} on a lookup error (transient; retried next cycle)
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
    dead = {"price": None, "ms": None, "reason": "bracket"}
    if parent is None:
        logger.error("No app bracket found for %s (ref %s)", symbol, ref)
        return dead
    legs = parent.get("legs") or []
    for child in legs:
        if child.get("status") != "filled":
            continue
        kind = {"stop": "stop", "limit": "target"}.get(child.get("type"))
        if kind is None:
            continue
        price = child.get("filled_avg_price")
        return {"price": float(price) if price not in (None, "") else None,
                "ms": _epoch_ms(child.get("filled_at")), "reason": kind}
    live_child = any(c.get("status") not in _TERMINAL and c.get("status") != "filled"
                     for c in legs)
    if parent.get("status") in _TERMINAL:
        # A partially filled then canceled entry still has live exits for the filled qty.
        try:
            entry_filled = float(parent.get("filled_qty") or 0) > 0
        except (TypeError, ValueError):
            entry_filled = False
        if entry_filled and live_child:
            return None
        logger.error("App bracket %s for %s ended %s without a live or filled exit",
                     parent.get("id"), symbol, parent.get("status"))
        return dead
    if parent.get("status") == "filled" and not live_child:
        logger.error("App bracket %s for %s: entry filled but no exit child is live or "
                     "filled", parent.get("id"), symbol)
        return dead
    return None


# --- kill switch ------------------------------------------------------------------
_UNREADABLE = "could not read the Alpaca broker (app positions unknown)"


def _fmt(net: dict[str, int]) -> str:
    return ", ".join(f"{sym} x{abs(q)}" for sym, q in sorted(net.items()))


async def _cancel_app_orders() -> tuple[int, list[str], list[dict] | None]:
    """Cancel every app-owned open order, one DELETE per owned id (never the bulk
    cancel-all), then wait for them to leave the book (open bracket legs reserve shares,
    so closes sent before cancels settle would reject).

    Returns (cancelled, failures, lingering): `lingering` is the app orders still live
    after the wait, or None when that could not be checked. A failed DELETE only counts
    if that order is still open afterwards: cancelling one OCO bracket leg ends its
    sibling, whose own DELETE then 422s."""
    from app.broker.ledger import app_open_orders
    orders, owned = await app_open_orders()
    cancelled = 0
    errors: dict[str, str] = {}
    for o in orders:
        oid = o.get("id")
        if oid not in owned:
            continue
        try:
            await ac.request("DELETE", f"/v2/orders/{oid}", write=True)
            cancelled += 1
        except (*_LOOKUP_ERRORS, PaperLockError) as exc:
            errors[oid] = str(exc)
            logger.warning("Kill switch: cancel of app order %s failed: %r", oid, exc)
    deadline = time.monotonic() + _CANCEL_WAIT
    while True:
        try:
            still, check_error = await _lingering(), None
        except _LOOKUP_ERRORS as exc:
            still, check_error = [], exc
        if (not still and check_error is None) or time.monotonic() >= deadline:
            break
        await asyncio.sleep(_CANCEL_STEP)
    if check_error is not None:
        return cancelled, [f"open-order check failed ({check_error})"], None
    failures = []
    for o in still:
        oid = o.get("id")
        why = f": {errors[oid]}" if oid in errors else ""
        failures.append(f"order {oid} ({o.get('symbol')}) still open{why}")
    return cancelled, failures, still


async def flatten_all() -> str:
    """Kill-switch flatten of what the APP opened on the shared paper account.

    Gate ON: paper lock (refused before any request) -> cancel app-owned open orders ->
    hold (not close) any symbol with an app order still live, or every symbol if that
    can't be checked -> for each other app net position, a tagged market DAY close for the app quantity capped at
    the account's same-direction position (options: single leg, *_to_close) -> poll the
    app ledger up to _FLATTEN_WAIT seconds. SAFE_MODE does not block it (closing only).
    Gate OFF: transmits nothing; raises if the app holds anything.
    Alpaca not configured (no keys): the app can never have transmitted, so it is flat
    by construction -> "No open positions." with no Alpaca call (gate ON or OFF).
    Raises RuntimeError("Flatten incomplete: ...") when anything app-owned remains or the
    configured broker cannot be read, so the kill switch never reports flat on a guess."""
    from app.broker.ledger import account_positions, app_net_positions
    if not ac.configured():
        return "No open positions."
    s = get_settings()
    if not s.enable_live_orders:
        try:
            app_net = await app_net_positions()
        except _LOOKUP_ERRORS as exc:
            raise RuntimeError(f"Flatten incomplete: {_UNREADABLE} ({exc})") from exc
        if not app_net:
            return "No open positions."
        logger.warning("[FLATTEN-PAPER] would close %s — ENABLE_LIVE_ORDERS off",
                       _fmt(app_net))
        raise RuntimeError(f"Flatten incomplete: not flat: {_fmt(app_net)} "
                           f"(paper: not transmitted)")
    if (blocked := _paper_lock_blocked()) is not None:
        raise RuntimeError(f"Flatten incomplete: {blocked['detail']}")

    failures: list[str] = []
    try:
        cancelled, cancel_failures, lingering = await _cancel_app_orders()
    except _LOOKUP_ERRORS as exc:
        cancelled, cancel_failures, lingering = 0, [f"open-order lookup failed ({exc})"], None
    failures += cancel_failures
    note = f"cancelled {cancelled} resting order(s); " if cancelled else ""
    try:
        app_net = await app_net_positions()
        acct = await account_positions()
    except _LOOKUP_ERRORS as exc:
        raise RuntimeError(f"Flatten incomplete: {note}{_UNREADABLE} ({exc}); "
                           f"{', '.join(failures) or 'no other failures'}") from exc

    from app.engine.order_store import record_intent
    closed: list[str] = []
    # shorts first: buying back a spread's short leg before selling its long keeps the
    # account covered while it unwinds
    for sym, app_qty in sorted(app_net.items(), key=lambda kv: (kv[1] > 0, kv[0])):
        # A still-live app order (e.g. a resting stop-sell) could fill on top of the
        # close; on the shared account that trades the user's manual shares. Hold.
        if lingering is None:
            failures.append(f"{sym} x{abs(app_qty)} (held: app open orders could not be "
                            f"confirmed cancelled)")
            continue
        if any(_matches(o, sym) for o in lingering):
            failures.append(f"{sym} x{abs(app_qty)} (held: resting app order not cleared)")
            continue
        ap = acct.get(sym) or {}
        acct_qty = int(ap.get("qty") or 0)
        if acct_qty == 0 or (acct_qty > 0) != (app_qty > 0):
            failures.append(f"{sym} x{abs(app_qty)} (account position differs: {acct_qty})")
            continue
        qty = min(abs(app_qty), abs(acct_qty))
        if qty < abs(app_qty):
            failures.append(f"{sym}: account holds {abs(acct_qty)} of the app's "
                            f"{abs(app_qty)}")
        side = "sell" if app_qty > 0 else "buy"
        option = ap.get("asset_class") == "us_option" or bool(_OCC.match(sym.upper()))
        try:
            ref = await record_intent("", sym, "exit_long" if app_qty > 0 else "exit_short",
                                      qty)
            out = await _transmit(
                _market_body(sym, qty, side, f"{side}_to_close" if option else None),
                ref, f"FLATTEN {side.upper()} {sym}")
        except Exception as exc:  # noqa: BLE001 — must attempt every position
            logger.error("Flatten failed for %s: %r", sym, exc)
            failures.append(f"{sym} x{qty} ({exc})")
            continue
        if out["transmitted"]:
            logger.warning("[FLATTEN-LIVE] %s %s x%d (ref %s)", side.upper(), sym, qty, ref)
            closed.append(f"{sym} x{qty}")
        else:
            failures.append(f"{sym} x{qty} ({out.get('detail')})")

    remaining = app_net
    if closed:
        deadline = time.monotonic() + _FLATTEN_WAIT
        while True:
            try:
                remaining, poll_error = await app_net_positions(), None
            except _LOOKUP_ERRORS as exc:
                poll_error = exc        # keep the last known `remaining`
            if (poll_error is None and not remaining) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(_FLATTEN_STEP)
        if poll_error is not None:
            failures.append(f"position check failed ({poll_error})")

    if remaining or failures:
        parts = []
        if remaining:
            parts.append(f"still open: {_fmt(remaining)} "
                         f"(orders may be queued until market open)")
        if failures:
            parts.append(f"failures: {', '.join(failures)}")
        raise RuntimeError(f"Flatten incomplete: {note}closed {len(closed)}/{len(app_net)} "
                           f"[{', '.join(closed) or 'none'}]; {'; '.join(parts)}")
    if not app_net:
        return f"{note}No open positions."
    return f"{note}Flattened {len(closed)} position(s): {', '.join(closed)}"
