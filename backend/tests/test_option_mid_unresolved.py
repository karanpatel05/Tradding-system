"""ib_option_quote must not call reqMktData on a contract IBKR couldn't resolve. A strike/expiry
combo that doesn't exist leaves conId=0 after qualify; the old code then called reqMktData,
which raises ValueError -> HTTP 500 in the builder /quote. It must degrade to None instead.
   Run: python -m pytest tests/test_option_mid_unresolved.py -q
"""
from __future__ import annotations

from datetime import date

import pytest

from app.ibkr import options_data


class _UnresolvedIB:
    """Mimics IBKR qualify for an unknown contract: returns the contract but leaves conId=0."""
    def __init__(self):
        self.reqmkt_called = False

    async def qualifyContractsAsync(self, c):
        return [c]  # unknown contract -> conId stays 0 (ib_async does not raise)

    def reqMktData(self, *a, **k):
        self.reqmkt_called = True
        raise AssertionError("reqMktData must not be called for an unqualified (conId=0) contract")

    def cancelMktData(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_ib_option_quote_returns_none_when_contract_unresolved(monkeypatch):
    fake = _UnresolvedIB()

    class _GW:
        async def get_ib(self):
            return fake

    monkeypatch.setattr(options_data, "get_gateway", lambda: _GW())
    out = await options_data.ib_option_quote("QQQ", date(2026, 7, 18), 500.0, "C")
    assert out is None, "unresolved option must yield None, not raise"
    assert fake.reqmkt_called is False, "must not request market data for an unqualified contract"
