"""Account + position access over IBKR, normalized to the shape the kill switch
and dashboard already consume (.symbol/.quantity/.quantity_direction/.instrument_type)."""
from __future__ import annotations

from dataclasses import dataclass

from app.ibkr.gateway import get_gateway

_SECTYPE_TO_INSTRUMENT = {"STK": "Equity"}


@dataclass
class NormalizedPosition:
    symbol: str
    quantity: int
    quantity_direction: str   # "Long" | "Short" | "Flat"
    instrument_type: str      # "Equity" | raw secType
    avg_price: float
    account: str


def normalize_position(p) -> NormalizedPosition:
    qty = int(p.position)
    direction = "Long" if qty > 0 else "Short" if qty < 0 else "Flat"
    sec = getattr(p.contract, "secType", "STK")
    return NormalizedPosition(
        symbol=p.contract.symbol, quantity=qty, quantity_direction=direction,
        instrument_type=_SECTYPE_TO_INSTRUMENT.get(sec, sec),
        avg_price=float(p.avgCost or 0.0), account=getattr(p, "account", ""))


async def get_accounts() -> list[str]:
    ib = await get_gateway().get_ib()
    return list(ib.managedAccounts())


async def get_positions(account: str | None = None) -> list[NormalizedPosition]:
    ib = await get_gateway().get_ib()
    out = [normalize_position(p) for p in ib.positions()]
    if account:
        out = [p for p in out if p.account == account]
    return [p for p in out if p.quantity != 0]


_ACCOUNT_TAGS = {
    "NetLiquidation": "net_liquidation", "TotalCashValue": "total_cash",
    "BuyingPower": "buying_power", "AvailableFunds": "available_funds",
    "GrossPositionValue": "gross_position_value", "UnrealizedPnL": "unrealized_pnl",
    "RealizedPnL": "realized_pnl", "InitMarginReq": "init_margin",
    "MaintMarginReq": "maint_margin",
}


def normalize_account_summary(values) -> dict:
    """IBKR accountSummary rows -> normalized {key: float|None} + account/currency."""
    out: dict = {key: None for key in _ACCOUNT_TAGS.values()}
    out["account"] = None
    out["currency"] = None
    for v in values:
        if out["account"] is None:
            out["account"] = getattr(v, "account", None) or None
        if out["currency"] is None and getattr(v, "currency", ""):
            out["currency"] = v.currency
        key = _ACCOUNT_TAGS.get(getattr(v, "tag", None))
        if key is not None:
            try:
                out[key] = float(v.value)
            except (TypeError, ValueError):
                pass
    return out


async def get_account_summary(account: str | None = None) -> dict:
    """Full IBKR account summary, normalized. Returns {} when not connected."""
    if not get_gateway().is_connected():
        return {}
    ib = await get_gateway().get_ib()
    rows = await ib.accountSummaryAsync(account or "")
    return normalize_account_summary(rows)
