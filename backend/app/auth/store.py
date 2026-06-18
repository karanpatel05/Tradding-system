"""Durable, server-side credential store for session persistence.

Writes the login-time refresh token, the (optional) panel-supplied OAuth client
secret, and the opaque ``app_session_id`` to a local JSON side-car so the session
survives a server restart — the user authenticates once, not on every reopen.

Security posture
----------------
These are **secrets at rest**. The repo's headline guarantee ("tokens stay
server-side, never returned to the browser") still holds — nothing here is ever
sent to the client — but credentials now live on disk instead of memory-only.
Mitigations: the file is written ``0600`` (owner read/write only) and git-ignored.
No local-key "encryption" is attempted; a key stored beside the ciphertext adds no
real protection. Disable entirely with ``PERSIST_SESSION=false``.

The refresh token is durable: the tastytrade SDK's ``Session.refresh()`` rotates
only the short-lived access token, never the refresh token — so the value stored
at login stays valid and never needs rewriting.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("trading.auth.store")


def _path() -> Path:
    return Path(get_settings().session_store_path)


def save(app_session_id: str, refresh_token: str, provider_secret: str | None) -> None:
    """Persist credentials with owner-only permissions (best-effort)."""
    if not get_settings().persist_session:
        return
    payload = json.dumps(
        {
            "app_session_id": app_session_id,
            "refresh_token": refresh_token,
            # Only set when panel-supplied; None means "fall back to env at rebuild".
            "provider_secret": provider_secret,
        }
    ).encode()
    path = _path()
    try:
        # Open with 0600 from the start so the file is never briefly world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(path, 0o600)  # tighten even if the file pre-existed with looser perms
        logger.info("[store-debug] session store WRITTEN ok -> %s (has_secret=%s)",
                    path, provider_secret is not None)
    except OSError as exc:
        logger.warning("Could not persist session store (%s): %s", path, exc)


def load() -> dict | None:
    """Return the stored credentials, or None if absent/unreadable."""
    path = _path()
    if not path.exists():
        logger.info("[store-debug] load: no file at %s", path)
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("refresh_token") and data.get("app_session_id"):
            logger.info("[store-debug] load: valid store found at %s", path)
            return data
        logger.info("[store-debug] load: file present but missing keys (rt=%s sid=%s)",
                    bool(data.get("refresh_token")), bool(data.get("app_session_id")))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable session store (%s): %s", path, exc)
    return None


def clear() -> None:
    """Delete the stored credentials (logout / failed restore)."""
    try:
        _path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not clear session store: %s", exc)
