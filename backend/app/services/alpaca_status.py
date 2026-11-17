"""Read-only Alpaca account status (display only — the app never trades via Alpaca).

Uses APCA_API_KEY_ID / APCA_API_SECRET_KEY from backend/.env and GETs /v2/account.
Results are cached briefly so the API tab's polling doesn't hammer Alpaca. Secrets
are never returned; the account number is masked."""
from __future__ import annotations

import time

import httpx

from app.config import get_settings

_CACHE_TTL = 30.0
_cache: dict = {"at": 0.0, "value": None}


def _mask(acct: str) -> str:
    return f"••••{acct[-4:]}" if acct and len(acct) > 4 else (acct or "")


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def fetch_alpaca_status(force: bool = False) -> dict:
    s = get_settings()
    key, secret = s.apca_api_key_id.strip(), s.apca_api_secret_key.strip()
    base = s.apca_api_base_url.rstrip("/")
    out = {"configured": bool(key and secret), "paper": "paper-api" in base}
    if not out["configured"]:
        return {**out, "status": "not_configured"}

    now = time.monotonic()
    if not force and _cache["value"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["value"]

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{base}/v2/account",
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        if r.status_code == 200:
            a = r.json()
            result = {**out, "status": "ok",
                      "account": _mask(str(a.get("account_number", ""))),
                      "account_status": a.get("status"),
                      "currency": a.get("currency"),
                      "equity": _num(a.get("equity")),
                      "cash": _num(a.get("cash")),
                      "buying_power": _num(a.get("buying_power"))}
        elif r.status_code in (401, 403):
            result = {**out, "status": "rejected", "detail": f"HTTP {r.status_code}: keys rejected"}
        else:
            result = {**out, "status": "error", "detail": f"HTTP {r.status_code}"}
    except httpx.HTTPError as exc:
        result = {**out, "status": "error", "detail": type(exc).__name__}

    _cache.update(at=now, value=result)
    return result
