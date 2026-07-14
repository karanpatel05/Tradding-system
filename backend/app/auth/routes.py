"""Connection routes (replaces Tastytrade OAuth login/credentials).

Auth is the IBKR Gateway connection: there are no secrets to submit. The frontend
shows connection config + status and can connect/disconnect the local Gateway."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.manager import session_manager
from app.config import get_settings
from app.ibkr.account import get_accounts
from app.tastytrade.session import reset_tt_session

logger = logging.getLogger("trading.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"   # backend/.env


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


class TTCredsBody(BaseModel):
    provider_secret: str | None = None
    refresh_token: str | None = None


def _upsert_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    for key, val in values.items():
        row = f"{key}={val}"
        for i, ln in enumerate(lines):
            if ln.startswith(f"{key}="):
                lines[i] = row
                break
        else:
            lines.append(row)
    path.write_text("\n".join(lines) + "\n")


def _tt_state() -> dict:
    from app.marketdata.facade import data_status
    s = get_settings()
    return {"configured": bool(s.tt_provider_secret and s.tt_refresh_token),
            "has_secret": bool(s.tt_provider_secret),
            "has_refresh": bool(s.tt_refresh_token),
            "data_primary": s.data_primary,
            **data_status()}


@router.get("/tastytrade")
async def tt_status() -> dict:
    return _tt_state()


@router.put("/tastytrade")
async def tt_save(body: TTCredsBody) -> dict:
    """Persist Tastytrade OAuth creds to backend/.env and apply them live (no restart)."""
    s = get_settings()
    updates: dict[str, str] = {}
    if body.provider_secret is not None:
        s.tt_provider_secret = body.provider_secret.strip()
        updates["TT_PROVIDER_SECRET"] = s.tt_provider_secret
    if body.refresh_token is not None:
        s.tt_refresh_token = body.refresh_token.strip()
        updates["TT_REFRESH_TOKEN"] = s.tt_refresh_token
    if updates:
        from app.providers import get_historical_provider

        _upsert_env(_ENV_PATH, updates)
        reset_tt_session()
        get_historical_provider.cache_clear()
    # Validate the pair against Tastytrade right away: a secret/refresh-token mismatch
    # ("invalid_grant") otherwise fails silently later, with everything on IBKR fallback.
    probe = None
    state = _tt_state()
    if state["configured"]:
        try:
            from app.tastytrade.session import get_tt_session
            sess = await get_tt_session()
            if sess is not None:
                await sess.refresh(force=True)
                probe = "ok"
        except Exception as e:  # noqa: BLE001 — surfaced to the UI, not raised
            probe = f"rejected: {str(e)[:200]}"
    return {**state, "probe": probe}
