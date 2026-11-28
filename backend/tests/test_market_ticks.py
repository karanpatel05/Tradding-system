import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tastytrade.dxfeed import TimeAndSale

from app.routers import ws
from app.tastytrade.streamer import TickSubscription, TTStreamer


class TickFeed:
    def __init__(self):
        self.calls = []
        self.events = asyncio.Queue()

    async def subscribe(self, cls, symbols, refresh_interval):
        self.calls.append(("add", cls, symbols, refresh_interval))

    async def unsubscribe(self, cls, symbols):
        self.calls.append(("remove", cls, symbols))

    async def listen(self, cls):
        assert cls is TimeAndSale
        while True:
            yield await self.events.get()


def sale(price, **overrides):
    fields = dict(event_symbol="SPY", time=1795796030000, price=price, size=0.5,
                  type="NEW", valid_tick=True, extended_trading_hours=False)
    return SimpleNamespace(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_pump_delivers_each_regular_sale_and_shares_subscriptions():
    s = TTStreamer()
    feed = s._dx = TickFeed()
    pump = asyncio.create_task(s._pump(TimeAndSale))
    try:
        async with s.listen_ticks(["SPY", "QQQ"]) as chart:
            async with s.listen_ticks(["SPY"]) as watchlist:
                assert feed.calls == [("add", TimeAndSale, ["SPY", "QQQ"], 0)]
                for ev in (sale(500), sale(501, type="CANCEL"), sale(502, valid_tick=False),
                           sale(503, extended_trading_hours=True), sale(504, type="CORRECTION"),
                           sale(510, size=None), sale(510, size=0),
                           sale(500), sale(509, event_symbol="QQQ"), sale(501.5)):
                    feed.events.put_nowait(ev)
                received = await asyncio.wait_for(chart.next_batch(), 1)
                assert [t["price"] for t in received] == [500, 502, 500, 509, 501.5]
                assert [t["updatesPrice"] for t in received] == [True, False, True, True, True]
                assert received[-1] == {"symbol": "SPY", "time": 1795796030000,
                                        "price": 501.5, "size": 0.5, "updatesPrice": True}
                assert await watchlist.next_batch() == [t for t in received if t["symbol"] == "SPY"]
            assert len(feed.calls) == 1
        assert feed.calls[-1] == ("remove", TimeAndSale, ["SPY", "QQQ"])
        assert not s._tick_listeners
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_overflow_only_interrupts_the_slow_browser(monkeypatch):
    monkeypatch.setattr(TickSubscription, "QUEUE_LIMIT", 1)
    s = TTStreamer()
    s._dx = TickFeed()
    async with s.listen_ticks(["SPY"]) as slow, s.listen_ticks(["SPY"]) as fast:
        for price in (200, 201, 202):
            s._publish_sale(sale(price))
            assert (await fast.next_batch())[0]["price"] == price
        assert slow.gapped and await slow.next_batch() == []
        assert not fast.gapped


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp,closing", [("2026-11-27T13:00:00-05:00", True),
                                            ("2026-11-25T13:00:00-05:00", False),
                                            ("2026-11-25T16:00:00-05:00", True)])
async def test_close_print_keeps_tape_time_and_belongs_to_final_bar(stamp, closing):
    feed = TTStreamer()
    feed._dx = TickFeed()
    time = int(datetime.fromisoformat(stamp).timestamp() * 1000)
    async with feed.listen_ticks(["SPY"]) as listener:
        feed._publish_sale(sale(500, time=time))
        [tick] = await listener.next_batch()
        assert tick["time"] == time
        assert tick.get("barTime", time) == time - int(closing)


@pytest.mark.asyncio
async def test_feed_drop_wakes_listener_and_fresh_client_resubscribes():
    s = TTStreamer()
    s._dx = TickFeed()
    async with s.listen_ticks(["SPY"]) as old:
        waiting = asyncio.create_task(old.next_batch())
        s._drop_state()
        assert await asyncio.wait_for(waiting, 1) == []
        feed = s._dx = TickFeed()
        async with s.listen_ticks(["SPY"]) as fresh:
            assert not fresh.gapped and feed.calls == [("add", TimeAndSale, ["SPY"], 0)]
            s._publish_sale(sale(210))
            assert (await fresh.next_batch())[0]["price"] == 210
        assert feed.calls[-1] == ("remove", TimeAndSale, ["SPY"])
    assert len(feed.calls) == 2


def test_fractional_time_and_sale_survives_sdk_decoder():
    fields = dict.fromkeys(TimeAndSale.model_fields, 0)
    fields.update(event_symbol="SPY", size=0.125, price=700, type="NEW", exchange_code="Q",
                  bid_price="NaN", ask_price="NaN", exchange_sale_conditions=None,
                  trade_through_exempt=" ", aggressor_side="BUY", buyer=None, seller=None)
    [ev] = TimeAndSale.from_stream(list(fields.values()))
    assert ev.size == 0.125


def test_public_socket_contract_and_cleanup(monkeypatch):
    requested, released = [], []
    tick = {"symbol": "SPY", "time": 1795796030000, "price": 350, "size": 10,
            "updatesPrice": True}

    @asynccontextmanager
    async def listen(symbols):
        requested.extend(symbols)
        listener = TickSubscription()
        listener.publish(tick)
        try:
            yield listener
        finally:
            released.extend(symbols)

    monkeypatch.setattr(ws, "get_streamer", lambda: SimpleNamespace(listen_ticks=listen))
    app = FastAPI()
    app.include_router(ws.router)
    with TestClient(app) as client:
        for symbols in ("", "BAD!", ",".join(f"A{i}" for i in range(21))):
            with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws/market?symbols=" + symbols):
                pass
            assert exc.value.code == 1008
        with client.websocket_connect("/ws/market?symbols=spy,SPY,QQQ") as socket:
            assert socket.receive_json() == {"type": "status", "connected": True}
            assert socket.receive_json() == {"type": "ticks", "ticks": [tick]}
    assert requested == released == ["SPY", "QQQ"]


def test_missing_feed_sends_offline_status_and_retry_close(monkeypatch):
    @asynccontextmanager
    async def listen(_symbols):
        listener = TickSubscription()
        listener.invalidate()
        yield listener

    monkeypatch.setattr(ws, "get_streamer", lambda: SimpleNamespace(listen_ticks=listen))
    app = FastAPI()
    app.include_router(ws.router)
    with TestClient(app).websocket_connect("/ws/market?symbols=SPY") as socket:
        assert socket.receive_json() == {"type": "status", "connected": False}
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
        assert exc.value.code == 1013
