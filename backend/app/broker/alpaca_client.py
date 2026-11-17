"""Low-level Alpaca HTTP client. Writes are hard-locked to the paper host; app-placed
orders carry a `tsys-` client_order_id prefix so they can be told apart from others on the
shared account. Keys are only ever sent as request headers — never logged or returned."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import get_settings

PAPER_HOST = "paper-api.alpaca.markets"
TAG_PREFIX = "tsys-"
_MAX_COID = 48
_TIMEOUT = 8.0


class PaperLockError(Exception):
    """Raised when a write would go to anything other than the Alpaca paper host."""


class AlpacaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Alpaca HTTP {status}: {message}")
        self.status = status
        self.message = message


def base_url() -> str:
    """Configured URL normalized to scheme+host (no trailing slash or /v2)."""
    raw = get_settings().apca_api_base_url.strip().rstrip("/")
    if raw.endswith("/v2"):
        raw = raw[: -len("/v2")]
    return raw.rstrip("/")


def is_paper_url(url: str) -> bool:
    """True only for https://paper-api.alpaca.markets exactly: https scheme, that exact
    host, no userinfo, default port. Shared by the write lock and the status chip."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return False
    return (u.scheme == "https" and u.hostname == PAPER_HOST
            and u.username is None and u.password is None and port in (None, 443))


def trading_base() -> str:
    base = base_url()
    if not is_paper_url(base):
        raise PaperLockError(f"writes are locked to https://{PAPER_HOST}")
    return base


def configured() -> bool:
    s = get_settings()
    return bool(s.apca_api_key_id.strip() and s.apca_api_secret_key.strip())


def headers() -> dict[str, str]:
    s = get_settings()
    return {"APCA-API-KEY-ID": s.apca_api_key_id.strip(),
            "APCA-API-SECRET-KEY": s.apca_api_secret_key.strip()}


async def request(method: str, path: str, *, params: dict | None = None,
                  json: dict | None = None, write: bool = False) -> Any:
    """Any non-GET is a write (paper-locked) whatever `write` says; `path` must be an
    Alpaca /v2/ path so it can never redirect the request to another host."""
    if not path.startswith("/v2/"):
        raise ValueError(f"Alpaca path must start with /v2/: {path!r}")
    write = write or method.upper() != "GET"
    base = trading_base() if write else base_url()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.request(method, f"{base}{path}", params=params, json=json,
                                 headers=headers())
    if not 200 <= r.status_code < 300:
        try:
            msg = str(r.json().get("message") or r.text)
        except Exception:  # noqa: BLE001 - non-JSON error body
            msg = r.text
        raise AlpacaError(r.status_code, msg[:300])
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def tag(ref: str) -> str:
    return (TAG_PREFIX + ref)[:_MAX_COID]


def is_app_owned(client_order_id: str | None) -> bool:
    return bool(client_order_id) and client_order_id.startswith(TAG_PREFIX)


def occ_unpadded(symbol: str) -> str:
    return symbol.replace(" ", "")
