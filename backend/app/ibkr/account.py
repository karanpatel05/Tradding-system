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
