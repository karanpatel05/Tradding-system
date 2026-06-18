"""Singleton IB Gateway connection manager (ib_async).

'Authenticated' in this system means the Gateway is connected and the API is
reachable. ``get_ib()`` returns a connected IB instance, connecting on first use.
``ib_factory`` is injectable so tests can stub the IB object."""
from __future__ import annotations

import logging
from typing import Callable

from app.config import get_settings

logger = logging.getLogger("trading.ibkr")


class Gateway:
    def __init__(self, ib_factory: Callable | None = None):
        self._ib_factory = ib_factory or self._default_factory
        self._ib = None

    @staticmethod
    def _default_factory():
        from ib_async import IB
        return IB()

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


_gateway = Gateway()


def get_gateway() -> Gateway:
    return _gateway
