"""Connection routes (replaces Tastytrade OAuth login/credentials).

Auth is the IBKR Gateway connection: there are no secrets to submit. The frontend
shows connection config + status and can connect/disconnect the local Gateway."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.manager import session_manager
from app.config import get_settings
from app.ibkr.account import get_accounts

logger = logging.getLogger("trading.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class ConnectionStatus(BaseModel):
    connected: bool
    host: str
    port: int
    client_id: int
    account: str
    accounts: list[str] = []
    detail: str | None = None
    conn_state: str = "disconnected"
    safe_mode: bool = False
    safe_mode_reason: str | None = None


async def _safe_mode_info() -> tuple[bool, str | None]:
    from app.db.base import session_scope
    from app.db.models import SystemFlag
    try:
        async with session_scope() as s:
            row = await s.get(SystemFlag, "safe_mode")
            if row and row.on:
                return True, row.reason or None
    except Exception:  # noqa: BLE001 — a status read must never 500 the badge
        logger.exception("safe-mode status read failed")
    return False, None


async def _status(detail: str | None = None) -> ConnectionStatus:
    from app.ibkr.gateway import get_gateway
    s = get_settings()
    accounts: list[str] = []
    if session_manager.is_authenticated():
        try:
            accounts = await get_accounts()
        except Exception:  # don't let a transient data error flip the view
            logger.exception("Failed to list IBKR accounts for status")
    sm_on, sm_reason = await _safe_mode_info()
    return ConnectionStatus(
        connected=session_manager.is_authenticated(),
        host=s.ibkr_host, port=s.ibkr_port, client_id=s.ibkr_client_id,
        account=s.ibkr_account, accounts=accounts, detail=detail,
        conn_state=get_gateway().state.value, safe_mode=sm_on, safe_mode_reason=sm_reason,
    )


@router.get("/status", response_model=ConnectionStatus)
async def status_endpoint() -> ConnectionStatus:
    """Ungated — the frontend polls this for the connection badge."""
    return await _status()


@router.post("/connect", response_model=ConnectionStatus)
async def connect() -> ConnectionStatus:
    """Connect to the local IB Gateway. Returns status (with a detail on failure)."""
    detail = None
    try:
        await session_manager.connect()
    except Exception as exc:  # noqa: BLE001 — Gateway down / wrong port / clientId in use
        logger.warning("IBKR connect failed: %s", exc)
        detail = (f"Could not connect to IB Gateway at "
                  f"{get_settings().ibkr_host}:{get_settings().ibkr_port} — {exc}")
    return await _status(detail)


@router.post("/disconnect", response_model=ConnectionStatus)
async def disconnect() -> ConnectionStatus:
    session_manager.disconnect()
    return await _status()
