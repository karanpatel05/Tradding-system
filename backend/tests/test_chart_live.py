import pytest

from app.services.chart_live import MAX_CHART_SYMBOLS, ChartLive


class FakeStreamer:
    def __init__(self, trades=None):
        self.trades = trades or {}
        self.subscribed = []

    async def _ensure(self):
        return True

    async def _subscribe(self, cls, symbol):
        self.subscribed.append((cls.__name__, symbol))
        return True

    def cached_trade(self, symbol):
        return self.trades.get(symbol, (None, None))


def make(session="regular", trades=None):
    s = FakeStreamer(trades)
    state = {"session": session}
    live = ChartLive(streamer=lambda: s, market_status=lambda: {"session": state["session"]})
    return live, s, state


@pytest.mark.asyncio
async def test_prices_only_in_regular_session():
    live, _, state = make("after_hours", {"SPY": (690.5, 1_700_000_000.25)})
    await live.set_symbols("a", ["SPY"])
    assert live.prices("a") == {}
    state["session"] = "regular"
    assert live.prices("a") == {"SPY": {"price": 690.5, "ts": 1_700_000_000_250}}


@pytest.mark.asyncio
async def test_symbols_are_normalized_capped_and_per_connection():
    live, s, _ = make(trades={"SPY": (1.0, 1.0)})
    extra = [f"S{chr(65 + i)}" for i in range(13)]
    await live.set_symbols("a", ["spy", "bad sym", *extra])
    assert live._symbols["a"][0] == "SPY"
    assert len(live._symbols["a"]) == MAX_CHART_SYMBOLS == 12
    assert "BAD SYM" not in live._symbols["a"]
    assert ("Trade", "SPY") in s.subscribed
    assert live.prices("a") == {"SPY": {"price": 1.0, "ts": 1000}}
    assert live.prices("b") == {}


@pytest.mark.asyncio
async def test_forget_clears_connection():
    live, _, _ = make(trades={"SPY": (1.0, 1.0)})
    await live.set_symbols("a", ["SPY"])
    live.forget("a")
    assert live.prices("a") == {}
    live.forget("a")  # idempotent


@pytest.mark.asyncio
async def test_subscribe_failure_never_raises():
    class Boom(FakeStreamer):
        async def _subscribe(self, cls, symbol):
            raise RuntimeError("dead")

    live = ChartLive(streamer=lambda: Boom(), market_status=lambda: {"session": "regular"})
    await live.set_symbols("a", ["SPY"])
    assert live.prices("a") == {}


@pytest.mark.asyncio
async def test_ws_receive_loop_handles_symbols_and_ignores_junk(monkeypatch):
    from fastapi import WebSocketDisconnect
    from app.routers import ws as ws_mod

    live, _, _ = make(trades={"SPY": (2.0, 2.0)})
    monkeypatch.setattr(ws_mod, "chart_live", live)
    frames = [ValueError("bad json"), ["list"], {"type": "other"},
              {"type": "chart_symbols", "symbols": ["spy"]}, WebSocketDisconnect()]

    class FakeWS:
        async def receive_json(self):
            f = frames.pop(0)
            if isinstance(f, Exception):
                raise f
            return f

    await ws_mod._receive_loop(FakeWS(), "c1")
    assert live.prices("c1") == {"SPY": {"price": 2.0, "ts": 2000}}
