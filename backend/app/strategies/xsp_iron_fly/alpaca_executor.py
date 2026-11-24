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

# Terminal order states. Anything else (including stopped, suspended, pending_cancel,
# an unexpected value or an empty status) counts as still working: treated as open, so
# a stuck order becomes "unknown" (SAFE_MODE) rather than "unfilled" (re-price).
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day",
                      "replaced"})
def option_tick(price: float) -> float:
    """Single-leg price increment for XSP options: $0.01 under $3.00, $0.05 from $3.00
    (Cboe penny-pilot style). A limit off-increment would be rejected."""
    return 0.01 if price < 3.0 - 1e-9 else 0.05


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
        """Send an order the executor described, through the app's shared order path:
        mleg -> ``orders.submit_combo_order`` (per-leg intents from the app ledger,
        signed limit), single leg -> ``orders.submit_option_order``. Both apply SAFE_MODE,
        the ENABLE_LIVE_ORDERS gate, the paper lock, the intent row and the tsys- tag."""
        from app.broker import orders
        if (why := self.routing_block()) is not None:
            return {"transmitted": False, "detail": f"blocked: {why}"}
        if body.get("order_class") == "mleg":
            legs, root = [], None
            for x in body["legs"]:
                parsed = orders.parse_occ(x["symbol"])
                if parsed is None:
                    return {"transmitted": False, "detail": f"bad OCC {x['symbol']}"}
                root, exp, right, strike = parsed
                legs.append({"expiry": exp, "strike": strike, "right": right,
                             "action": "SELL" if x["side"] == "sell" else "BUY"})
            limit = float(body["limit_price"])
            out = await orders.submit_combo_order(
                root, legs, net_price=abs(limit), action="SELL" if limit < 0 else "BUY",
                reason=f"{self.key} {intent}", quantity=int(qty), strategy_key=self.key)
        else:
            out = await orders.submit_option_order(
                body["symbol"], body["side"], int(qty), limit_price=float(body["limit_price"]),
                position_intent=body["position_intent"], strategy_key=self.key,
                reason=intent)
        if out.get("paper"):        # the gate turned off between checks: nothing was sent
            out = {**out, "transmitted": False}
        return out

    async def find_by_ref(self, ref: str) -> tuple[str, dict | None]:
        """("found", order) | ("missing", None) for a 404 | ("error", None) when the
        lookup itself failed (which proves nothing about the order)."""
        try:
            o = await ac.request("GET", "/v2/orders:by_client_order_id",
                                 params={"client_order_id": ac.tag(ref)})
        except AlpacaError as exc:
            return ("missing", None) if exc.status == 404 else ("error", None)
        except httpx.HTTPError:
            return "error", None
        return ("found", o) if o and o.get("id") else ("missing", None)

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
        """App-owned parent orders that are not terminal (by ``TERMINAL``, so
        pending_cancel / stopped / suspended count as working). Raises on a read error."""
        from app.broker.ledger import app_orders
        return [o for o in await app_orders("all") if status_of(o) not in TERMINAL]


def occ(leg: dict) -> str:
    return ac.occ_unpadded(leg.get("occ") or leg["symbol"])


class AlpacaFlyExecutor:
    name = "alpaca_paper"
    supports_combo = True

    def __init__(self, port, feed, clock, *, window: float = 4.0, step: float = 0.5,
                 cancel_wait: float = 6.0, lookup_wait: float = 4.0) -> None:
        self.port, self.feed, self.clock = port, feed, clock
        self.window, self.step, self.cancel_wait = window, step, cancel_wait
        self.lookup_wait = lookup_wait
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

    async def _submit(self, body: dict, intent: str,
                      qty: int) -> tuple[str | None, str, bool]:
        """(order_id, why_not, unknown). ``unknown`` = the order may exist at Alpaca but
        couldn't be identified: the caller must fail closed (never resend)."""
        out = await self.port.submit(body, intent, qty)
        if out.get("transmitted") and out.get("order_id"):
            return str(out["order_id"]), "", False
        if out.get("unknown_state") or out.get("transmitted"):   # landed, id unknown
            ref = out.get("ref")
            if not ref:
                return None, f"order state unknown, no ref: {out.get('detail')}", True
            # The POST may have landed: look it up by its tagged id for a few seconds.
            # A 404 may just be lag; a failed lookup proves nothing. Either way, if it
            # stays unresolved the state is unknown, never "rejected".
            deadline = self.clock.now().timestamp() + self.lookup_wait
            last = "lookup not attempted"
            while True:
                kind, o = await self.port.find_by_ref(ref)
                if kind == "found" and o:
                    return str(o["id"]), "", False
                last = "not found" if kind == "missing" else "lookup failed"
                if self.clock.now().timestamp() >= deadline:
                    break
                await self.clock.sleep(self.step)
            why = "transmitted without an order id" if out.get("transmitted") else \
                "order state unknown after POST"
            return None, f"{why} ({last}; ref {ref})", True
        return None, str(out.get("detail") or "not transmitted"), False

    async def _settle_order(self, order_id: str) -> dict | None:
        """Poll until terminal or the window ends, then cancel and re-read the final
        state (a fill can land during the cancel)."""
        self._working.add(order_id)
        deadline = self.clock.now().timestamp() + self.window
        o = None
        while True:
            o = await self.port.get_order(order_id) or o
            if o is not None and status_of(o) in TERMINAL:
                break
            if self.clock.now().timestamp() >= deadline:
                o = await self._cancel_confirmed(order_id) or o
                break
            await self.clock.sleep(self.step)
        if o is not None and status_of(o) in TERMINAL:
            self._working.discard(order_id)
        return o

    async def _cancel_confirmed(self, order_id: str) -> dict | None:
        await self.port.cancel(order_id)
        o = None
        for _ in range(max(1, int(self.cancel_wait / self.step))):
            o = await self.port.get_order(order_id) or o
            if o is not None and status_of(o) in TERMINAL:
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
        oid, why, unknown = await self._submit(body, "fly_open", qty)
        if oid is None:
            return OrderResult("unknown" if unknown else "rejected",
                               detail=f"combo not placed: {why}")
        o = await self._settle_order(oid)
        if o is None:
            return OrderResult("unknown", detail=f"combo {oid}: order unreadable; state "
                                                 f"unknown (reconcile before trading)")
        n = filled_qty(o)
        if status_of(o) not in TERMINAL:     # cancel didn't take: it may still fill
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
                           f"{body['limit_price']}", order_id=oid)

    async def single(self, leg: dict, side: str, qty: int, limit: float, *,
                     max_age: float | None = None) -> OrderResult:
        mids = self._mids([leg], max_age)
        if isinstance(mids, str):
            return OrderResult("unfilled", detail=mids)
        opening = side == leg["action"]
        px = L.round_to_tick(limit, option_tick(limit), "up" if side == "BUY" else "down")
        if side == "SELL":
            px = max(px, 0.01)
        if px <= 0:
            return OrderResult("unfilled", detail=f"no valid {side} limit ({limit})")
        intent = f"{'buy' if side == 'BUY' else 'sell'}_to_{'open' if opening else 'close'}"
        body = {"symbol": occ(leg), "qty": str(int(qty)), "side": side.lower(),
                "type": "limit", "time_in_force": "day", "limit_price": f"{px:.2f}",
                "position_intent": intent}
        oid, why, unknown = await self._submit(body, f"leg_{intent}", qty)
        if oid is None:
            return OrderResult("unknown" if unknown else "rejected",
                               detail=f"{intent} not placed: {why}")
        o = await self._settle_order(oid)
        if o is None:
            return OrderResult("unknown", detail=f"{intent} {oid}: order unreadable; state "
                                                 f"unknown (reconcile before trading)")
        n = filled_qty(o)
        if status_of(o) not in TERMINAL:     # cancel didn't take: it may still fill
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
                           f"{fill if fill is not None else '-'} (limit {px:.2f})",
                           order_id=oid)

    async def cancel_all(self) -> int:
        """Cancel every order this executor left working (normally none)."""
        n = 0
        for oid in list(self._working):
            o = await self._cancel_confirmed(oid)
            if o is None or status_of(o) in TERMINAL:
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

    async def cancel_working_for(self, legs: list[dict]) -> tuple[int, str | None]:
        """Cancel app-owned open orders on this session's symbols (left by a crash).
        Returns (cancelled, problem). A failed listing or an unconfirmed cancel is a
        problem: the caller fails closed (an old stop may still be live)."""
        syms = {occ(l) for l in legs}
        try:
            orders = await self.port.open_orders()
        except Exception as exc:  # noqa: BLE001
            return 0, f"could not list open Alpaca orders: {type(exc).__name__}: {exc}"
        n, stuck = 0, []
        for o in orders:
            touched = {ac.occ_unpadded(x.get("symbol") or "") for x in o.get("legs") or []}
            touched.add(ac.occ_unpadded(o.get("symbol") or ""))
            if touched & syms and o.get("id"):
                final = await self._cancel_confirmed(str(o["id"]))
                if final is None or status_of(final) not in TERMINAL:
                    stuck.append(str(o["id"]))
                n += 1
        return n, (f"cancel not confirmed for {stuck}" if stuck else None)
