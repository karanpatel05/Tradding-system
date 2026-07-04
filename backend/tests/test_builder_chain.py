import pytest

from app.options import chain


class _Chain:
    exchange = "SMART"
    expirations = {"20260731", "20260724", "20260807"}
    strikes = {495.0, 500.0, 505.0}


class _FakeIB:
    async def qualifyContractsAsync(self, *a):
        return list(a)

    async def reqSecDefOptParamsAsync(self, symbol, ftp, sectype, conid):
        return [_Chain()]


@pytest.mark.asyncio
async def test_get_option_chain_sorts_and_isoformats(monkeypatch):
    class _GW:
        async def get_ib(self):
            return _FakeIB()
    monkeypatch.setattr(chain, "get_gateway", lambda: _GW())
    out = await chain.get_option_chain("qqq")
    assert out["underlying"] == "QQQ"
    assert out["expirations"] == ["2026-07-24", "2026-07-31", "2026-08-07"]
    assert out["strikes"] == [495.0, 500.0, 505.0]
