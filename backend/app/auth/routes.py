"""Connection routes (Tastytrade OAuth probe status; no login/credentials flow).

Auth is a Tastytrade OAuth-pair readiness probe: there is no interactive login, only
a provider secret + refresh token verified against Tastytrade. The frontend shows
connection status and can force a re-probe via /connect."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.gate import require_auth
from app.auth.manager import session_manager
from app.broker.status import is_ready_cached, probe
from app.config import get_settings
from app.tastytrade.session import reset_tt_session

logger = logging.getLogger("trading.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"   # backend/.env


class ConnectionStatus(BaseModel):
    connected: bool
    host: str | None = None
    port: int | None = None
    client_id: int | None = None
    account: str = ""
    accounts: list[str] = []
    detail: str | None = None
    conn_state: str = "disconnected"
    safe_mode: bool = False
    safe_mode_reason: str | None = None


def _mask_account(number: str) -> str:
    if not number:
        return ""
    if len(number) <= 4:
        return number
    return "•" * (len(number) - 4) + number[-4:]


async def _account_number() -> str:
    try:
        from app.broker.account import balances
        return (await balances()).get("account") or ""
    except Exception:  # noqa: BLE001 — a status read must never 500 the badge
        logger.exception("Failed to read Alpaca account for status")
        return ""


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


async def _status() -> ConnectionStatus:
    result = await probe()
    detail = result if (result and result != "ok") else None
    masked = _mask_account(await _account_number())
    sm_on, sm_reason = await _safe_mode_info()
    conn_state = "safe_mode" if sm_on else ("ready" if is_ready_cached() else "disconnected")
    return ConnectionStatus(
        connected=is_ready_cached(),
        host=None, port=None, client_id=None,
        account=masked, accounts=[masked] if masked else [], detail=detail,
        conn_state=conn_state, safe_mode=sm_on, safe_mode_reason=sm_reason,
    )


@router.get("/status", response_model=ConnectionStatus)
async def status_endpoint() -> ConnectionStatus:
    """Ungated — the frontend polls this for the connection badge."""
    return await _status()


@router.post("/connect", response_model=ConnectionStatus)
async def connect() -> ConnectionStatus:
    """Force a fresh Tastytrade OAuth probe. Returns status (detail set on rejection)."""
    try:
        await session_manager.connect()
    except Exception as exc:  # noqa: BLE001 — probe() itself doesn't raise; defensive
        logger.warning("Tastytrade probe failed: %s", exc)
    return await _status()


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
            **data_status()}


@router.get("/tastytrade")
async def tt_status(probe: bool = False) -> dict:
    from app.broker import status as _bstatus
    return {**_tt_state(), "probe": await _bstatus.probe(force=probe)}


@router.get("/alpaca")
async def alpaca_status(probe: bool = False) -> dict:
    """Read-only Alpaca account status for the API tab (no secrets returned)."""
    from app.services.alpaca_status import fetch_alpaca_status
    return await fetch_alpaca_status(force=probe)


@router.put("/tastytrade", dependencies=[Depends(require_auth)])
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
    # ("invalid_grant") otherwise fails silently later, with everything on fallback.
    return {**_tt_state(), "probe": await probe(force=True)}
