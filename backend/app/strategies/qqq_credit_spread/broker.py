"""Alpaca PAPER execution for the QQQ vertical. Tastytrade is never sent an order.

Every order goes through the app's own transmit path (``broker.orders._transmit``):
SAFE_MODE -> ENABLE_LIVE_ORDERS gate -> paper lock -> durable intent row -> POST with
``client_order_id = tsys-<ref>``. The paper account is shared, so the strategy only
counts what its OWN tagged orders filled (``broker.ledger.app_net_positions``).

Alpaca mleg sign convention (verified by the external runner on this paper account,
2026-11-03): ``limit_price`` NEGATIVE = the least net CREDIT accepted (opening a credit
spread); POSITIVE = the most net DEBIT paid (closing it). A credit sent as a positive
number fills at any price at all.

Re-pricing is cancel-confirmed then resubmit with a fresh tagged id (never PATCH): a
PATCHed replacement is not guaranteed to keep the ``tsys-`` tag, and an untagged fill
would vanish from the app ledger.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.broker import alpaca_client as ac
from app.broker.alpaca_client import AlpacaError

logger = logging.getLogger("trading.qqq_spread.broker")

OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held",
                 "accepted_for_bidding", "pending_cancel", "pending_replace", "calculated"}
TERMINAL_UNFILLED = {"canceled", "expired", "rejected", "suspended", "done_for_day"}


def signed_limit(price: float, opening: bool) -> float:
    p = round(abs(float(price)), 2)
    return -p if opening else p


def spread_body(short_occ: str, long_occ: str, qty: int, price: float, opening: bool) -> dict:
    legs = ([{"symbol": short_occ, "ratio_qty": "1", "side": "sell",
              "position_intent": "sell_to_open"},
             {"symbol": long_occ, "ratio_qty": "1", "side": "buy",
              "position_intent": "buy_to_open"}] if opening else
            [{"symbol": short_occ, "ratio_qty": "1", "side": "buy",
              "position_intent": "buy_to_close"},
             {"symbol": long_occ, "ratio_qty": "1", "side": "sell",
              "position_intent": "sell_to_close"}])
    return {"order_class": "mleg", "qty": str(int(qty)), "type": "limit",
            "time_in_force": "day", "limit_price": f"{signed_limit(price, opening):.2f}",
            "legs": legs}


def order_status(o: dict | None) -> str:
    return str((o or {}).get("status") or "").lower()


def filled_qty(o: dict | None) -> int:
    try:
        return int(round(float((o or {}).get("filled_qty") or 0)))
    except (TypeError, ValueError):
        return 0


def net_fill_price(o: dict | None, opening: bool) -> float | None:
    """Per-share net fill, positive. Alpaca reports a received credit as negative."""
    v = (o or {}).get("filled_avg_price")
    if v in (None, ""):
        return None
    try:
        return abs(float(v))
    except (TypeError, ValueError):
        return None


def leg_fill_prices(o: dict | None) -> dict[str, float]:
    out = {}
    for leg in (o or {}).get("legs") or []:
        sym = ac.occ_unpadded(leg.get("symbol") or "")
        v = leg.get("filled_avg_price")
        if sym and v not in (None, ""):
            try:
                out[sym] = abs(float(v))
            except (TypeError, ValueError):
                pass
    return out


class AlpacaSpreadBroker:
    """The runner's order port. Tests substitute a fake with the same methods."""

    name = "alpaca_paper"

    def __init__(self, strategy_key: str) -> None:
        self.key = strategy_key

    def routing_block(self) -> str | None:
        """Why no order can reach Alpaca paper right now, or None."""
        from app.config import get_settings
        if not get_settings().enable_live_orders:
            return "order routing is off (ENABLE_LIVE_ORDERS) - nothing would reach Alpaca paper"
        if not ac.configured():
            return "Alpaca paper keys are not configured"
        try:
            ac.trading_base()
        except ac.PaperLockError as exc:
            return f"paper lock: {exc}"
        return None

    async def submit_spread(self, short_occ: str, long_occ: str, qty: int, price: float,
                            opening: bool) -> dict:
        from app.broker.orders import _transmit
        from app.engine.order_store import record_intent
        from app.engine.safe_mode import is_safe_mode
        if qty < 1:
            return {"transmitted": False, "detail": "quantity < 1"}
        if await is_safe_mode():
            return {"transmitted": False, "safe_mode": True, "detail": "blocked: SAFE_MODE"}
        if (why := self.routing_block()) is not None:
            return {"transmitted": False, "detail": f"blocked: {why}"}
        body = spread_body(short_occ, long_occ, qty, price, opening)
        ref = await record_intent(self.key, "QQQ", "spread_open" if opening else "spread_close",
                                  int(qty))
        what = f"QQQ spread {'open' if opening else 'close'} x{qty} @ {body['limit_price']}"
        out = await _transmit(body, ref, what)
        logger.warning("[ALPACA PAPER] %s -> %s", what, out)
        return out

    async def get_order(self, order_id: str) -> dict | None:
        try:
            return await ac.request("GET", f"/v2/orders/{order_id}",
                                    params={"nested": "true"})
        except (AlpacaError, httpx.HTTPError) as exc:
            logger.warning("order %s read failed: %r", order_id, exc)
            return None

    async def cancel_confirmed(self, order_id: str, wait: float = 6.0,
                               step: float = 0.25) -> dict | None:
        """Cancel and wait until the order is terminal. Returns the final order (which
        may show a fill that landed during the cancel), or None if unreadable."""
        try:
            await ac.request("DELETE", f"/v2/orders/{order_id}", write=True)
        except AlpacaError as exc:
            # 422: already filled/cancelled - the read below tells which.
            logger.info("cancel %s: %s", order_id, exc)
        except httpx.HTTPError as exc:
            logger.warning("cancel %s transport error: %r", order_id, exc)
        loops = max(1, int(wait / step))
        o = None
        for _ in range(loops):
            o = await self.get_order(order_id)
            if o is not None and order_status(o) not in OPEN_STATUSES:
                return o
            await asyncio.sleep(step)
        return o

    async def leg_quantities(self, short_occ: str, long_occ: str) -> tuple[int, int]:
        """(short qty, long qty) the APP holds in these legs, from its own tagged orders.
        Raises on an unreadable ledger: the caller must not act on a guess."""
        from app.broker.ledger import app_net_positions
        net = await app_net_positions()
        return max(0, -int(net.get(short_occ, 0))), max(0, int(net.get(long_occ, 0)))

    async def working_orders(self, symbols: set[str]) -> list[dict]:
        """App-owned open orders touching any of these option symbols."""
        from app.broker.ledger import app_open_orders
        orders, _ = await app_open_orders()
        out = []
        for o in orders:
            syms = {ac.occ_unpadded(l.get("symbol") or "") for l in o.get("legs") or []}
            syms.add(ac.occ_unpadded(o.get("symbol") or ""))
            if syms & symbols:
                out.append(o)
        return out
