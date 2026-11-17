"""Reconcile broker truth (positions/fills/open orders) against local records."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_price: float


@dataclass
class BrokerFill:
    symbol: str
    qty: int
    price: float
    perm_id: str | None
    exec_id: str


@dataclass
class BrokerOrder:
    symbol: str
    perm_id: str | None
    order_ref: str | None
    status: str


async def _positions():
    from app.broker.account import positions
    return await positions()


async def _live_orders():
    from app.broker.account import live_orders
    return await live_orders()


async def fetch_snapshot() -> dict:
    """Broker truth from Tastytrade: positions + live orders (fills come with order
    history and are not needed for the position reconcile — kept as an empty list to
    preserve the snapshot shape)."""
    positions = [BrokerPosition(p.symbol, p.quantity, p.avg_price)
                 for p in await _positions() if p.quantity != 0]
    orders = []
    for o in await _live_orders():
        def g(x, k, d=None):
            return x.get(k, d) if isinstance(x, dict) else getattr(x, k, d)
        legs = g(o, "legs", []) or []
        sym = g(legs[0], "symbol", "?") if legs else (g(o, "symbol") or "?")
        orders.append(BrokerOrder(sym, str(g(o, "id", "")) or None, None,
                                  str(g(o, "status", ""))))
    return {"positions": positions, "fills": [], "orders": orders}


@dataclass
class ReconResult:
    ok: bool
    discrepancies: list[str]


def reconcile(broker_positions: list[BrokerPosition], local_open: dict[str, int]) -> ReconResult:
    discrepancies: list[str] = []
    broker = {p.symbol: p.qty for p in broker_positions}
    for sym, bqty in broker.items():
        lqty = local_open.get(sym, 0)
        if lqty != bqty:
            discrepancies.append(f"{sym}: broker {bqty} vs local {lqty}")
    for sym, lqty in local_open.items():
        if lqty != 0 and sym not in broker:
            discrepancies.append(f"{sym}: local {lqty} vs broker flat")
    return ReconResult(ok=not discrepancies, discrepancies=discrepancies)
