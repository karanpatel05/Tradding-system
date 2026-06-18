"""Tastytrade live-data provider (quotes / streaming / accounts / positions /
orders). This is a thin delegation layer over the existing ``tastytrade/client``
so the live side has a symmetric provider seam. Phase 1 keeps the existing live
call sites using the client directly; this wrapper documents the boundary and is
the place future live routing consolidates into. It deliberately holds NO
historical methods — historical data comes only from Massive.
"""

from __future__ import annotations

from app.tastytrade.client import get_client


class TastytradeLiveProvider:
    """Live market data + trading surface, backed by Tastytrade."""

    name = "tastytrade"

    def __init__(self):
        self._client = get_client()

    # Live quotes (one-shot DXLink snapshot).
    async def get_quotes(self, symbols, timeout: float = 4.0):
        return await self._client.get_quotes(symbols, timeout=timeout)

    # Account / positions (used by the dashboard, kill switch, execution).
    async def get_accounts(self):
        return await self._client.get_accounts()

    async def get_positions(self, account):
        return await self._client.get_positions(account)
