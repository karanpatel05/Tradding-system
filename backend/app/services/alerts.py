"""Pluggable operator alerts. Ships in-app only (journal + log); webhook/email can be
added later via register_channel without touching call sites. Never raises into callers."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from app.services.journal import log_event

logger = logging.getLogger("trading.alerts")

AlertChannel = Callable[[str, str, "dict | None"], Awaitable[None]]
_CHANNELS: list[AlertChannel] = []
_ERROR_KINDS = {"safe_mode", "reconnect_failed"}


def register_channel(ch: AlertChannel) -> None:
    _CHANNELS.append(ch)


async def send_alert(kind: str, message: str, data: dict | None = None) -> None:
    for ch in list(_CHANNELS):
        try:
            await ch(kind, message, data)
        except Exception:  # noqa: BLE001 — one bad channel must not block the others
            logger.exception("alert channel failed (%s)", kind)


def fire_alert(kind: str, message: str, data: dict | None = None) -> None:
    """Sync entry point for non-async callers (event handlers). Best-effort."""
    try:
        asyncio.get_running_loop().create_task(send_alert(kind, message, data))
    except RuntimeError:
        pass  # no running loop (e.g. a sync test) — skip scheduling


async def _journal_channel(kind: str, message: str, data: dict | None) -> None:
    level = "error" if kind in _ERROR_KINDS else "warning"
    await log_event(f"[{kind}] {message}", category="system", level=level, data=data)
    logger.warning("ALERT %s: %s", kind, message)


register_channel(_journal_channel)
