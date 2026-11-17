"""Reconcile broker truth (positions/fills/open orders) against local records."""
from __future__ import annotations

from dataclasses import dataclass

from app.broker import alpaca_client, ledger


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


class BrokerUnavailable(Exception):
    """The Alpaca ledger / account positions could not be read."""


def _account_issues(net: dict[str, int], account: dict[str, dict]) -> list[str]:
    """App-owned net the shared account cannot cover: the account holds less of the same
    sign, nothing, or the opposite sign. A larger same-sign account position is fine (the
    excess is someone else's manual position)."""
    issues = []
    for sym, app_qty in net.items():
        acct = int((account.get(sym) or {}).get("qty", 0))
        if acct * app_qty <= 0 or abs(acct) < abs(app_qty):
            issues.append(f"{sym}: account {acct} < app-owned {app_qty}")
    return issues


def _order_symbol(o: dict) -> str:
    legs = o.get("legs") or []
    sym = o.get("symbol") or (legs[0].get("symbol") if legs else "") or "?"
    return alpaca_client.occ_unpadded(sym)


async def fetch_snapshot() -> dict:
    """Broker truth = the app's own Alpaca ledger (tagged orders only): app-owned net
    positions (avg price from the account), app-owned open orders, and `issues` where the
    shared account holds less than the app owns. Untagged positions are ignored. Alpaca
    unconfigured -> empty snapshot (nothing can have been transmitted)."""
    if not alpaca_client.configured():
        return {"positions": [], "fills": [], "orders": [], "issues": []}
    try:
        net = await ledger.app_net_positions()
        account = await ledger.account_positions()
        open_orders, _ = await ledger.app_open_orders()
    except Exception as exc:  # noqa: BLE001 - any read failure must fail safe, not open
        raise BrokerUnavailable(f"{type(exc).__name__}: {exc}") from exc
    positions = [BrokerPosition(sym, qty, float((account.get(sym) or {})
                                                .get("avg_entry_price", 0.0)))
                 for sym, qty in net.items()]
    orders = [BrokerOrder(_order_symbol(o), o.get("id"), o.get("client_order_id"),
                          str(o.get("status") or "")) for o in open_orders]
    return {"positions": positions, "fills": [], "orders": orders,
            "issues": _account_issues(net, account)}


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
