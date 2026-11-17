"""Alpaca-backed account views (app-owned positions/orders only), normalized to the shapes the dashboard/kill-switch already
consume. The real Account object is threaded everywhere (SDK gotcha: it has many required
fields and cannot be rebuilt from account_number). Reads never raise — they degrade to
None/{}/[] and the caller/UI handles absence."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.broker import alpaca_client as ac
from app.broker.alpaca_client import AlpacaError
from app.broker.ledger import account_positions, app_net_positions, app_orders

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


def _mask(number: str | None) -> str:
    return f"••••{number[-4:]}" if number and len(number) > 4 else (number or "")


_ERRORS = (AlpacaError, httpx.HTTPError)


async def balances() -> dict:
    try:
        a = await ac.request("GET", "/v2/account")
    except _ERRORS:
        logger.warning("Alpaca balance fetch failed", exc_info=True)
        return {}
    if not isinstance(a, dict):
        return {}
    return {
        "net_liquidation": _f(a.get("equity")),
        "total_cash": _f(a.get("cash")),
        "buying_power": _f(a.get("buying_power")),
        "available_funds": _f(a.get("buying_power")),
        "gross_position_value": None,
        "unrealized_pnl": None,
        "realized_pnl": None,
        "init_margin": None,
        "maint_margin": None,
        "account": _mask(a.get("account_number")),
        "currency": "USD",
    }


async def positions() -> list[NormalizedPosition]:
    try:
        ledger = await app_net_positions()
        if not ledger:
            return []
        acct_pos = await account_positions()
    except _ERRORS:
        logger.warning("Alpaca positions fetch failed", exc_info=True)
        return []
    try:
        a = await ac.request("GET", "/v2/account")
    except _ERRORS:
        logger.warning("Alpaca account number unavailable for positions", exc_info=True)
        a = None
    account = _mask((a or {}).get("account_number")) if isinstance(a, dict) else ""
    out: list[NormalizedPosition] = []
    for sym, qty in sorted(ledger.items()):
        ap = acct_pos.get(sym) or {}
        opt = ap.get("asset_class") == "us_option"
        out.append(NormalizedPosition(
            symbol=sym, quantity=qty, quantity_direction="Long" if qty > 0 else "Short",
            instrument_type="Equity Option" if opt else "Equity",
            avg_price=float(ap.get("avg_entry_price") or 0.0), account=account))
    return out


async def live_orders() -> list[dict]:
    try:
        return await app_orders("open")
    except _ERRORS:
        logger.warning("Alpaca live-orders fetch failed", exc_info=True)
        return []


async def cancel_order(order_id: str) -> bool:
    try:
        owned = {o.get("id") for o in await app_orders("open")}
        if order_id not in owned:
            logger.warning("Refusing to cancel non-app order %s", order_id)
            return False
        await ac.request("DELETE", f"/v2/orders/{order_id}", write=True)
        return True
    except (*_ERRORS, ac.PaperLockError):
        logger.warning("Cancel failed for order %s", order_id, exc_info=True)
        return False
