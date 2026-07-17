"""Tastytrade account access, normalized to the shapes the dashboard/kill-switch already
consume. The real Account object is threaded everywhere (SDK gotcha: it has many required
fields and cannot be rebuilt from account_number). Reads never raise — they degrade to
None/{}/[] and the caller/UI handles absence."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("trading.broker.account")

_account = None
_lock = asyncio.Lock()


@dataclass
class NormalizedPosition:
    symbol: str
    quantity: int
    quantity_direction: str   # "Long" | "Short" | "Flat"
    instrument_type: str      # "Equity" | "Equity Option" | raw TT type
    avg_price: float
    account: str


async def _get_session():
    from app.tastytrade.session import get_tt_session
    return await get_tt_session()


async def _fetch_account(session):
    from tastytrade import Account

    from app.config import get_settings
    number = get_settings().tt_account_number or None
    got = await Account.get(session, number) if number else await Account.get(session)
    if isinstance(got, list):
        return got[0] if got else None
    return got


async def get_tt_account(refresh: bool = False):
    global _account
    if _account is not None and not refresh:
        return _account
    session = await _get_session()
    if session is None:
        return None
    async with _lock:
        if _account is None or refresh:
            try:
                _account = await _fetch_account(session)
            except Exception:  # noqa: BLE001 — degrade; probe/UI surfaces broker health
                logger.warning("Account fetch failed", exc_info=True)
                _account = None
    return _account


def reset_tt_account() -> None:
    global _account
    _account = None


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def balances() -> dict:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return {}
    try:
        b = await a.get_balances(session)
    except Exception:  # noqa: BLE001
        logger.warning("Balance fetch failed", exc_info=True)
        return {}
    return {
        "net_liquidation": _f(getattr(b, "net_liquidating_value", None)),
        "total_cash": _f(getattr(b, "cash_balance", None)),
        "buying_power": _f(getattr(b, "equity_buying_power", None)),
        "available_funds": _f(getattr(b, "derivative_buying_power", None)),
        "gross_position_value": None,
        "unrealized_pnl": None,          # not in TT balances; positions carry marks
        "realized_pnl": None,
        "init_margin": _f(getattr(b, "margin_equity", None)),
        "maint_margin": None,
        "account": getattr(a, "account_number", None),
        "currency": "USD",
    }


async def positions() -> list[NormalizedPosition]:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return []
    try:
        rows = await a.get_positions(session, include_marks=True)
    except TypeError:
        rows = await a.get_positions(session)
    except Exception:  # noqa: BLE001
        logger.warning("Positions fetch failed", exc_info=True)
        return []
    out: list[NormalizedPosition] = []
    for p in rows:
        qty = int(p.quantity)
        if getattr(p, "quantity_direction", "") == "Short":
            qty = -abs(qty)
        if qty == 0:
            continue
        direction = "Long" if qty > 0 else "Short"
        out.append(NormalizedPosition(
            symbol=str(p.symbol), quantity=qty, quantity_direction=direction,
            instrument_type=str(getattr(p, "instrument_type", "Equity")),
            avg_price=_f(getattr(p, "average_open_price", 0)) or 0.0,
            account=str(getattr(p, "account_number", ""))))
    return out


async def live_orders() -> list:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return []
    try:
        return list(await a.get_live_orders(session))
    except Exception:  # noqa: BLE001
        return []


async def cancel_order(order_id: int) -> bool:
    a = await get_tt_account()
    session = await _get_session()
    if a is None or session is None:
        return False
    try:
        await a.delete_order(session, int(order_id))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Cancel failed for order %s", order_id, exc_info=True)
        return False
