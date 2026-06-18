import pytest

from app.ibkr import gateway as gw


class _FakeIB:
    def __init__(self):
        self._conn = False

    def isConnected(self):
        return self._conn

    async def connectAsync(self, host, port, clientId, readonly=False):
        self.host, self.port, self.clientId = host, port, clientId
        self._conn = True

    def reqMarketDataType(self, t):
        self.mdt = t

    def disconnect(self):
        self._conn = False


@pytest.mark.asyncio
async def test_connect_sets_connected_and_market_data_type():
    fake = _FakeIB()
    g = gw.Gateway(ib_factory=lambda: fake)
    assert g.is_connected() is False
    await g.connect()
    assert g.is_connected() is True
    assert fake.mdt == 1            # from settings default
    assert fake.port == 4002        # paper Gateway default
    g.disconnect()
    assert g.is_connected() is False


@pytest.mark.asyncio
async def test_connect_is_idempotent():
    fake = _FakeIB()
    g = gw.Gateway(ib_factory=lambda: fake)
    await g.connect()
    await g.connect()               # second call no-ops while connected
    assert g.is_connected() is True
