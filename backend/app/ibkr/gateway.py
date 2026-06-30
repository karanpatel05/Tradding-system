"""Singleton IB Gateway connection manager (ib_async).

'Authenticated' in this system means the Gateway is connected and the API is
reachable. ``get_ib()`` returns a connected IB instance, connecting on first use.
``ib_factory`` is injectable so tests can stub the IB object.

Beyond a bare connection this owns the **connection-state machine**
(``ConnState``), disconnect detection (ib_async events + IBKR 1100/1101/1102
system codes), an exponential-backoff reconnect loop, and a heartbeat watchdog.
Trading is permitted ONLY in ``ConnState.READY`` (see the worker pause guard and
the recovery sequence)."""
from __future__ import annotations

import asyncio
import enum
import logging
import random
from typing import Callable

from app.config import get_settings

logger = logging.getLogger("trading.ibkr")


class ConnState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECOVERING = "recovering"
    READY = "ready"
    SAFE_MODE = "safe_mode"


class Gateway:
    def __init__(self, ib_factory: Callable | None = None):
        self._ib_factory = ib_factory or self._default_factory
        self._ib = None
        self._state = ConnState.DISCONNECTED
        self._subscribers: list[Callable[[ConnState], None]] = []
        self.data_lost = False

    @staticmethod
    def _default_factory():
        from ib_async import IB
        return IB()

    # --- connection-state machine ----------------------------------------
    @property
    def state(self) -> ConnState:
        return self._state

    def subscribe(self, cb: "Callable[[ConnState], None]") -> None:
        self._subscribers.append(cb)

    def set_state(self, s: ConnState) -> None:
        if s is self._state:
            return
        self._state = s
        logger.warning("Connection state -> %s", s.value)
        for cb in list(self._subscribers):
            try:
                cb(s)
            except Exception:  # noqa: BLE001 — a bad subscriber must not break transitions
                logger.exception("conn-state subscriber failed")

    # --- event wiring ----------------------------------------------------
    def _wire_events(self, ib) -> None:
        ib.disconnectedEvent += self._on_disconnect
        ib.errorEvent += self._on_error

    def _on_disconnect(self, *_) -> None:
        self.set_state(ConnState.DISCONNECTED)
        from app.services.alerts import fire_alert
        fire_alert("disconnect", "IBKR Gateway disconnected")

    def _on_error(self, reqId, code, msg, *_) -> None:
        if code == 1100:
            self.set_state(ConnState.DISCONNECTED)
        elif code == 1101:
            self.data_lost = True
        # 1102 = restored, data maintained → nothing to flag here.

    # --- connection ------------------------------------------------------
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def connect(self):
        if self.is_connected():
            return self._ib
        s = get_settings()
        ib = self._ib or self._ib_factory()
        await ib.connectAsync(s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, readonly=False)
        ib.reqMarketDataType(s.ibkr_market_data_type)
        self._ib = ib
        self._wire_events(ib)
        self.set_state(ConnState.CONNECTED)
        logger.warning("Connected to IB Gateway %s:%s clientId=%s",
                       s.ibkr_host, s.ibkr_port, s.ibkr_client_id)
        return ib

    async def get_ib(self):
        if not self.is_connected():
            await self.connect()
        return self._ib

    def disconnect(self):
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    # --- reconnect + heartbeat ------------------------------------------
    @staticmethod
    def backoff_schedule(attempt: int) -> float:
        return min(60.0, float(2 ** attempt))

    async def reconnect_loop(self, max_attempts: int | None = None) -> None:
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            try:
                self.set_state(ConnState.CONNECTING)
                await self.connect()
                return
            except Exception:  # noqa: BLE001 — keep retrying through any connect failure
                delay = self.backoff_schedule(attempt) + random.uniform(0, 0.5)
                logger.warning("reconnect attempt %d failed; retrying in %.1fs", attempt, delay)
                await asyncio.sleep(delay)
                attempt += 1
                if attempt == 5:
                    from app.services.alerts import fire_alert
                    fire_alert("reconnect_failed",
                               f"reconnect still failing after {attempt} attempts")

    async def heartbeat_once(self, timeout: float = 5.0) -> bool:
        if self._ib is None:
            return False
        try:
            await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 — timeout or socket error both mean "dead"
            logger.warning("heartbeat missed; marking DISCONNECTED")
            self.set_state(ConnState.DISCONNECTED)
            return False

    # --- recovery orchestration -----------------------------------------
    async def connect_and_recover(self) -> bool:
        from app.engine.recovery import run_recovery  # lazy: avoid import cycle
        await self.connect()
        return await run_recovery(self._ib, self)

    async def supervise(self, interval: float = 15.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._supervise_tick()

    async def _supervise_tick(self) -> None:
        from app.engine.recovery import run_recovery  # lazy: avoid import cycle
        if self.state is ConnState.DISCONNECTED:
            await self.reconnect_loop()
            await run_recovery(self._ib, self)
        elif self.is_connected() and self.state is ConnState.CONNECTED:
            await run_recovery(self._ib, self)  # reconnected without recovery → drive to READY
        elif self.is_connected():
            await self.heartbeat_once()


_gateway = Gateway()


def get_gateway() -> Gateway:
    return _gateway
