"""Alpaca PAPER executor for the XSP iron fly: real orders, real fills.

Every order goes through the app's own Alpaca path (``broker.orders._transmit``):
SAFE_MODE -> ENABLE_LIVE_ORDERS gate -> paper lock -> durable intent row -> POST with
``client_order_id = tsys-<ref>``. The paper account is shared, so only the app's own
tagged orders count (``broker.ledger``). Tastytrade is never sent an order.

Index options do not accept IOC/FOK, so every order is a DAY limit that is polled for
``window`` seconds and then cancelled (cancel confirmed, final state re-read: a fill can
land during the cancel). The result carries Alpaca's own per-leg average fill prices;
nothing is assumed. ``order_intents`` is not used for fill state (its status is never
advanced); the order is always read back from Alpaca.

Sign convention for mleg limits (verified on this paper account, see
``qqq_credit_spread/broker.py``): NEGATIVE limit_price = the least net credit accepted.
"""
from __future__ import annotations

import logging

import httpx

from app.broker import alpaca_client as ac
from app.broker.alpaca_client import AlpacaError
from app.strategies.xsp_iron_fly import logic as L
from app.strategies.xsp_iron_fly.executor import OrderResult

logger = logging.getLogger("trading.xsp_fly.alpaca")

OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held",
                 "accepted_for_bidding", "pending_cancel", "pending_replace", "calculated"}
SINGLE_TICK = 0.05     # single-leg limits rounded to a nickel: never rejected for increment


def _f(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def status_of(o: dict | None) -> str:
    return str((o or {}).get("status") or "").lower()


def filled_qty(o: dict | None) -> int:
    return int(round(_f((o or {}).get("filled_qty")) or 0))


class AlpacaPort:
    """Thin port over the app's Alpaca path. Tests substitute a fake with these methods."""

    def __init__(self, strategy_key: str) -> None:
        self.key = strategy_key

    def routing_block(self) -> str | None:
        from app.config import get_settings
        if not get_settings().enable_live_orders:
            return "order routing is off (ENABLE_LIVE_ORDERS): nothing would reach Alpaca paper"
        if not ac.configured():
            return "Alpaca paper keys are not configured"
        try:
            ac.trading_base()
        except ac.PaperLockError as exc:
            return f"paper lock: {exc}"
        return None

    async def submit(self, body: dict, intent: str, qty: int) -> dict:
        from app.broker.orders import _transmit
        from app.engine.order_store import record_intent
        from app.engine.safe_mode import is_safe_mode
        if await is_safe_mode():
            return {"transmitted": False, "safe_mode": True, "detail": "blocked: SAFE_MODE"}
        if (why := self.routing_block()) is not None:
            return {"transmitted": False, "detail": f"blocked: {why}"}
        ref = await record_intent(self.key, "XSP", intent, int(qty))
        out = await _transmit(body, ref, f"XSP fly {intent} x{qty}")
        logger.warning("[ALPACA PAPER] XSP %s x%s %s -> %s", intent, qty,
                       body.get("limit_price"), out)
        return out

    async def find_by_ref(self, ref: str) -> dict | None:
        try:
            return await ac.request("GET", "/v2/orders:by_client_order_id",
                                    params={"client_order_id": ac.tag(ref)})
        except (AlpacaError, httpx.HTTPError):
            return None

    async def get_order(self, order_id: str) -> dict | None:
        try:
            return await ac.request("GET", f"/v2/orders/{order_id}", params={"nested": "true"})
        except (AlpacaError, httpx.HTTPError) as exc:
            logger.warning("order %s read failed: %r", order_id, exc)
            return None

    async def cancel(self, order_id: str) -> None:
        try:
            await ac.request("DELETE", f"/v2/orders/{order_id}", write=True)
        except AlpacaError as exc:          # 422: already filled/cancelled
            logger.info("cancel %s: %s", order_id, exc)
        except httpx.HTTPError as exc:
            logger.warning("cancel %s transport error: %r", order_id, exc)

    async def positions(self, *, include_expired: bool = False) -> dict[str, int]:
        """App-owned net qty per OCC symbol (raises on an unreadable ledger)."""
        from app.broker.ledger import app_net_positions
        return await app_net_positions(include_expired=include_expired)

    async def open_orders(self) -> list[dict]:
        from app.broker.ledger import app_open_orders
        orders, _ = await app_open_orders()
        return orders


def occ(leg: dict) -> str:
    return ac.occ_unpadded(leg.get("occ") or leg["symbol"])


class AlpacaFlyExecutor:
    name = "alpaca_paper"
    supports_combo = True

    def __init__(self, port, feed, clock, *, window: float = 4.0, step: float = 0.5,
                 cancel_wait: float = 6.0) -> None:
        self.port, self.feed, self.clock = port, feed, clock
        self.window, self.step, self.cancel_wait = window, step, cancel_wait
        self._working: set[str] = set()

    # --- helpers -------------------------------------------------------------
    def _mids(self, legs: list[dict], max_age: float | None) -> dict[str, float] | str:
        """Feed mids for slippage logging, and the freshness guard: never send an order
        decided on a stale quote."""
        out = {}
        for leg in legs:
            q = self.feed.quote(leg["symbol"])
            if q is None or not L.quote_valid(q.bid, q.ask):
                return f"no quote for {leg['symbol']}"
            if max_age is not None:
                age = q.age(self.clock.now())
                if age is None or age > max_age:
                    return f"stale quote for {leg['symbol']} (age {age}s > {max_age:g}s)"
            out[leg["role"]] = L.mid(q.bid, q.ask)
        return out

    async def _submit(self, body: dict, intent: str, qty: int) -> tuple[str | None, str]:
        out = await self.port.submit(body, intent, qty)
        if out.get("transmitted") and out.get("order_id"):
            return out["order_id"], ""
        if out.get("unknown_state") and out.get("ref"):
            # The POST may have landed: look it up by its tagged id before giving up.
            o = await self.port.find_by_ref(out["ref"])
            if o and o.get("id"):
                return str(o["id"]), ""
        return None, str(out.get("detail") or "not transmitted")

    async def _settle_order(self, order_id: str) -> dict | None:
        """Poll until terminal or the window ends, then cancel and re-read the final
        state (a fill can land during the cancel)."""
        self._working.add(order_id)
        deadline = self.clock.now().timestamp() + self.window
        o = None
        while True:
            o = await self.port.get_order(order_id) or o
            if o is not None and status_of(o) not in OPEN_STATUSES:
                break
            if self.clock.now().timestamp() >= deadline:
                o = await self._cancel_confirmed(order_id) or o
                break
            await self.clock.sleep(self.step)
        if o is not None and status_of(o) not in OPEN_STATUSES:
            self._working.discard(order_id)
        return o

    async def _cancel_confirmed(self, order_id: str) -> dict | None:
        await self.port.cancel(order_id)
        o = None
        for _ in range(max(1, int(self.cancel_wait / self.step))):
            o = await self.port.get_order(order_id) or o
            if o is not None and status_of(o) not in OPEN_STATUSES:
                return o
            await self.clock.sleep(self.step)
        logger.error("XSP order %s still open after cancel", order_id)
        return o

    # --- executor protocol -----------------------------------------------------
    async def combo(self, legs: list[dict], qty: int, limit_credit: float, *,
                    max_age: float | None = None) -> OrderResult:
        mids = self._mids(legs, max_age)
        if isinstance(mids, str):
            return OrderResult("unfilled", detail=mids)
        if limit_credit <= 0:
            return OrderResult("rejected", detail=f"non-positive credit limit {limit_credit}")
        body = {"order_class": "mleg", "qty": str(int(qty)), "type": "limit",
                "time_in_force": "day",
                "limit_price": f"{-round(abs(limit_credit), 2):.2f}",   # negative = credit
                "legs": [{"symbol": occ(l), "ratio_qty": "1",
                          "side": "sell" if l["action"] == "SELL" else "buy",
                          "position_intent": ("sell_to_open" if l["action"] == "SELL"
                                              else "buy_to_open")} for l in legs]}
        oid, why = await self._submit(body, "fly_open", qty)
        if oid is None:
            return OrderResult("rejected", detail=f"combo not placed: {why}")
        o = await self._settle_order(oid)
        if o is None:
            return OrderResult("unknown", detail=f"combo {oid}: order unreadable; state "
                                                 f"unknown (reconcile before trading)")
        n = filled_qty(o)
        if status_of(o) in OPEN_STATUSES:     # cancel didn't take: it may still fill
            return OrderResult("unknown", filled_qty=n,
                               detail=f"combo {oid} still {status_of(o)} after cancel; "
                                      "not sending another order")
        by_sym = {ac.occ_unpadded(x.get("symbol") or ""): x for x in o.get("legs") or []}
        prices = {}
        for l in legs:
            px = _f((by_sym.get(occ(l)) or {}).get("filled_avg_price"))
            if px is not None:
                prices[l["role"]] = abs(px)
        if n and len(prices) < len(legs):
            return OrderResult("unknown", filled_qty=n,
                               detail=f"combo {oid} filled {n} but leg prices missing: "
                                      f"{sorted(prices)}; reconcile")
        status = "filled" if n >= qty else ("partial" if n else "unfilled")
        return OrderResult(status, n, prices, mids,
                           f"alpaca combo {oid} {status_of(o)} {n}/{qty} at limit "
                           f"{body['limit_price']}")

    async def single(self, leg: dict, side: str, qty: int, limit: float, *,
                     max_age: float | None = None) -> OrderResult:
        mids = self._mids([leg], max_age)
        if isinstance(mids, str):
            return OrderResult("unfilled", detail=mids)
        opening = side == leg["action"]
        px = L.round_to_tick(limit, SINGLE_TICK, "up" if side == "BUY" else "down")
        if px <= 0:
            return OrderResult("unfilled", detail=f"no valid {side} limit ({limit})")
        intent = f"{'buy' if side == 'BUY' else 'sell'}_to_{'open' if opening else 'close'}"
        body = {"symbol": occ(leg), "qty": str(int(qty)), "side": side.lower(),
                "type": "limit", "time_in_force": "day", "limit_price": f"{px:.2f}",
                "position_intent": intent}
        oid, why = await self._submit(body, f"leg_{intent}", qty)
        if oid is None:
            return OrderResult("rejected", detail=f"{intent} not placed: {why}")
        o = await self._settle_order(oid)
        if o is None:
            return OrderResult("unknown", detail=f"{intent} {oid}: order unreadable; state "
                                                 f"unknown (reconcile before trading)")
        n = filled_qty(o)
        if status_of(o) in OPEN_STATUSES:     # cancel didn't take: it may still fill
            return OrderResult("unknown", filled_qty=n,
                               detail=f"{intent} {oid} still {status_of(o)} after cancel; "
                                      "not sending another order")
        fill = _f(o.get("filled_avg_price"))
        if n and fill is None:
            return OrderResult("unknown", filled_qty=n,
                               detail=f"{intent} {oid} filled {n} without a price; reconcile")
        status = "filled" if n >= qty else ("partial" if n else "unfilled")
        return OrderResult(status, n, {leg["role"]: abs(fill)} if n else {}, mids,
                           f"alpaca {intent} {oid} {status_of(o)} {n}/{qty} @ "
                           f"{fill if fill is not None else '-'} (limit {px:.2f})")

    async def cancel_all(self) -> int:
        """Cancel every order this executor left working (normally none)."""
        n = 0
        for oid in list(self._working):
            o = await self._cancel_confirmed(oid)
            if o is None or status_of(o) not in OPEN_STATUSES:
                self._working.discard(oid)
            n += 1
        return n

    # --- reconciliation hooks (used by the runner) -----------------------------
    async def check_positions(self, expected: dict[str, int]) -> list[str]:
        """Compare the session's expected net quantity per OCC symbol (from its own
        order fills) with what the app's tagged Alpaca fills say. Expired contracts are
        included: this checks a specific session's symbols, before or after the close.
        Returns discrepancies (empty = match)."""
        try:
            held = await self.port.positions(include_expired=True)
        except Exception as exc:  # noqa: BLE001 - unreadable broker fails closed
            return [f"Alpaca ledger unreadable: {type(exc).__name__}: {exc}"]
        return [f"{sym}: Alpaca {int(held.get(sym, 0)):+d} vs session {want:+d}"
                for sym, want in sorted(expected.items()) if int(held.get(sym, 0)) != want]

    async def cancel_working_for(self, legs: list[dict]) -> int:
        """Cancel app-owned open orders on this session's symbols (left by a crash)."""
        syms = {occ(l) for l in legs}
        try:
            orders = await self.port.open_orders()
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for o in orders:
            touched = {ac.occ_unpadded(x.get("symbol") or "") for x in o.get("legs") or []}
            touched.add(ac.occ_unpadded(o.get("symbol") or ""))
            if touched & syms and o.get("id"):
                await self._cancel_confirmed(str(o["id"]))
                n += 1
        return n
