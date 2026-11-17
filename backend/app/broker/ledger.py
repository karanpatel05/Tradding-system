"""App-owned position ledger. The Alpaca paper account is shared, so positions/orders are
derived only from orders whose client_order_id carries the `tsys-` tag (fills of those
orders and their nested children), never from the account's raw positions."""
from __future__ import annotations

from app.broker.alpaca_client import is_app_owned, occ_unpadded, request

_PAGE = 500
_MAX_PAGES = 20


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
    return out


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
            ratio = _qty(leg.get("ratio_qty")) or 1.0
            sym = occ_unpadded(leg.get("symbol") or "")
            if sym:
                net[sym] = net.get(sym, 0.0) + sign * ratio * _qty(leg.get("filled_qty"))
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
