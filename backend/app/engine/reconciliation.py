"""Reconcile broker truth (positions/fills/open orders) against local records."""
from __future__ import annotations

from dataclasses import dataclass

from ib_async import ExecutionFilter


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


async def fetch_snapshot(ib) -> dict:
    positions = [
        BrokerPosition(p.contract.symbol, int(p.position), float(p.avgCost or 0.0))
        for p in await ib.reqPositionsAsync() if int(p.position) != 0
    ]
    fills = []
    for f in await ib.reqExecutionsAsync(ExecutionFilter()):
        ex = f.execution
        fills.append(BrokerFill(f.contract.symbol, int(ex.shares), float(ex.price),
                                str(ex.permId) if ex.permId else None, ex.execId))
    orders = [
        BrokerOrder(t.contract.symbol,
                    str(t.order.permId) if t.order.permId else None,
                    t.order.orderRef or None, t.orderStatus.status)
        for t in await ib.reqAllOpenOrdersAsync()
    ]
    return {"positions": positions, "fills": fills, "orders": orders}


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
