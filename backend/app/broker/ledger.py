"""App-owned position ledger. The Alpaca paper account is shared, so positions/orders are
derived only from orders whose client_order_id carries the `tsys-` tag (fills of those
orders and their nested children), never from the account's raw positions."""
from __future__ import annotations

import logging

from app.broker.alpaca_client import AlpacaError, is_app_owned, occ_unpadded, request

logger = logging.getLogger("trading.broker.ledger")

_PAGE = 500
_MAX_PAGES = 20


class LedgerIncomplete(AlpacaError):
    """The app's order history could not be read in full (page cap hit). An AlpacaError so
    every caller treats it like an unreadable broker: fail closed, never act on a partial
    ledger (a dropped old fill can show a phantom position)."""

    def __init__(self, message: str):
        super().__init__(0, message)


async def app_orders(status: str = "all") -> list[dict]:
    """App-owned parent orders (children nested), newest first, paged by `until`."""
    out: list[dict] = []
    seen: set[str] = set()
    until: str | None = None
    for _ in range(_MAX_PAGES):
        params = {"status": status, "nested": "true", "limit": _PAGE, "direction": "desc"}
        if until:
            params["until"] = until
        page = await request("GET", "/v2/orders", params=params) or []
        fresh = [o for o in page if o.get("id") not in seen]
        for o in fresh:
            seen.add(o.get("id"))
            if is_app_owned(o.get("client_order_id")):
                out.append(o)
        if len(page) < _PAGE or not fresh:
            break
        until = page[-1].get("submitted_at") or page[-1].get("created_at")
        if not until:
            break
    else:
        logger.error("Alpaca order paging hit the %d-page cap; older app orders are "
                     "missing, refusing to use a partial ledger", _MAX_PAGES)
        raise LedgerIncomplete(f"order history exceeds the {_MAX_PAGES}-page cap; "
                               f"app ledger incomplete")
    return out


OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held",
                 "accepted_for_bidding", "pending_replace"}


def _is_open(o: dict) -> bool:
    return o.get("status") in OPEN_STATUSES


async def app_open_orders() -> tuple[list[dict], set[str]]:
    """App-owned open orders as dicts, plus the set of cancellable app-owned ids.

    Resting bracket legs of a filled tagged parent can come back with Alpaca-generated
    (untagged) client ids, so ownership is derived from tagged parents (nested=true).
    An open parent is returned whole (cancelling it cancels its legs); for a closed
    parent each open nested leg is returned on its own. mleg legs are never separate."""
    orders: list[dict] = []
    ids: set[str] = set()
    for parent in await app_orders("all"):
        legs = parent.get("legs") or []
        if _is_open(parent):
            orders.append(parent)
            ids.add(parent.get("id"))
            if parent.get("order_class") != "mleg":
                ids.update(l.get("id") for l in legs)
        elif parent.get("order_class") != "mleg":
            for leg in legs:
                if _is_open(leg):
                    orders.append(leg)
                    ids.add(leg.get("id"))
    ids.discard(None)
    return orders, ids


def _qty(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _accumulate(o: dict, net: dict[str, float]) -> None:
    legs = o.get("legs") or []
    if o.get("order_class") == "mleg":
        for leg in legs:
            sign = 1 if leg.get("side") == "buy" else -1
            sym = occ_unpadded(leg.get("symbol") or "")
            if sym:
                net[sym] = net.get(sym, 0.0) + sign * _qty(leg.get("filled_qty"))
        return
    sym = occ_unpadded(o.get("symbol") or "")
    if sym:
        sign = 1 if o.get("side") == "buy" else -1
        net[sym] = net.get(sym, 0.0) + sign * _qty(o.get("filled_qty"))
    for child in legs:
        _accumulate(child, net)


async def app_net_positions() -> dict[str, int]:
    net: dict[str, float] = {}
    for o in await app_orders("all"):
        _accumulate(o, net)
    return {k: int(round(v)) for k, v in net.items() if int(round(v)) != 0}


async def account_positions() -> dict[str, dict]:
    rows = await request("GET", "/v2/positions") or []
    out: dict[str, dict] = {}
    for p in rows:
        qty = int(_qty(p.get("qty")))
        if p.get("side") == "short":
            qty = -abs(qty)
        out[occ_unpadded(p.get("symbol") or "")] = {
            "qty": qty, "avg_entry_price": _qty(p.get("avg_entry_price")),
            "asset_class": p.get("asset_class") or ""}
    return out
