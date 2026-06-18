"""Massive (Polygon) historical provider + routing tests.

Run: python -m tests.test_massive_provider

Uses httpx.MockTransport so no network/key is needed. Verifies:
  • normalization (Polygon agg → our bar shape), all timeframes, adjusted=true
  • timestamp→ET-session mapping is correct (the silent-bug guard)
  • ascending sort + pagination via next_url + 429 backoff
  • historical reads route through Massive; live client has no historical method
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx

from app.providers.base import MassiveNotConfigured
from app.providers.massive import MassiveHistoricalProvider

# 2024-06-03 13:35:00 UTC == 09:35:00 America/New_York (EDT) — the session open.
_MS_0935_ET = 1_717_421_700_000


def _provider(handler, **kw):
    return MassiveHistoricalProvider("TEST_KEY", "https://api.polygon.io",
                                     retry_base=0.0, transport=httpx.MockTransport(handler), **kw)


async def _run() -> None:
    # --- normalization + adjusted=true + timeframe mapping ---
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["adjusted"] = req.url.params.get("adjusted")
        return httpx.Response(200, json={"results": [
            {"t": _MS_0935_ET, "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5, "v": 12345},
        ], "next_url": None})

    bars = await _provider(handler).get_bars("AAPL", "5m", start=date(2024, 6, 3), end=date(2024, 6, 3))
    assert len(bars) == 1, bars
    b = bars[0]
    assert b == {"time": _MS_0935_ET, "open": 10.0, "high": 11.0, "low": 9.5,
                 "close": 10.5, "volume": 12345, "symbol": "AAPL"}, b
    assert seen["adjusted"] == "true", seen
    assert "/range/5/minute/2024-06-03/2024-06-03" in seen["url"], seen["url"]
    print("PASS: normalizes Polygon aggs → bar shape, 5m mapping, adjusted=true")

    # daily timeframe maps to /1/day
    def dayh(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"results": [{"t": _MS_0935_ET, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})
    await _provider(dayh).get_bars("AAPL", "1d", start=date(2024, 1, 1))
    assert "/range/1/day/" in seen["url"], seen["url"]
    print("PASS: 1d maps to /1/day")

    # --- timestamp → ET-session mapping (the silent-bug guard) ---
    from app.strategies.ema_scalper.params import EmaScalperParams
    from app.strategies.ema_scalper.runner import is_session_open, is_past_cutoff
    from app.backtest.engine import _bar_date

    assert _bar_date({"time": _MS_0935_ET}).isoformat() == "2024-06-03", _bar_date({"time": _MS_0935_ET})
    p = EmaScalperParams.from_settings()
    assert is_session_open(_MS_0935_ET, p) is True, "09:35 ET should be inside the session"
    assert is_past_cutoff(_MS_0935_ET, p) is False, "09:35 ET is well before the cutoff"
    # 15:50 ET (after the 15:45 cutoff) == 19:50 UTC
    ms_1550_et = _MS_0935_ET + (6 * 3600 + 15 * 60) * 1000
    assert is_past_cutoff(ms_1550_et, p) is True, "15:50 ET should be past the cutoff"
    print("PASS: Polygon ms timestamps map to the correct ET calendar date + session edges")

    # --- ascending sort + pagination ---
    pages = iter([
        {"results": [{"t": 3000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}],
         "next_url": "https://api.polygon.io/next?cursor=abc"},
        {"results": [{"t": 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                     {"t": 2000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], "next_url": None},
    ])
    def paged(req):
        return httpx.Response(200, json=next(pages))
    bars = await _provider(paged).get_bars("AAPL", "1m", start=date(2024, 6, 3))
    assert [x["time"] for x in bars] == [1000, 2000, 3000], bars  # merged + sorted asc
    print("PASS: follows next_url pagination and sorts ascending")

    # --- 429 backoff then success ---
    state = {"n": 0}
    def ratelimited(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"results": [{"t": 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})
    bars = await _provider(ratelimited).get_bars("AAPL", "5m", start=date(2024, 6, 3))
    assert state["n"] == 2 and len(bars) == 1, (state, bars)
    print("PASS: retries once on 429 then succeeds")

    # --- no API key → typed error (graceful-degradation contract) ---
    try:
        await MassiveHistoricalProvider(None, "https://api.polygon.io").get_bars("AAPL", "5m")
    except MassiveNotConfigured:
        print("PASS: raises MassiveNotConfigured without an API key")
    else:
        raise AssertionError("expected MassiveNotConfigured")

    # --- routing: historical goes through Massive; live client has no historical ---
    from app.providers import get_historical_provider
    assert get_historical_provider().name == "massive"
    from app.tastytrade.client import TastytradeClient
    assert not hasattr(TastytradeClient, "get_candles"), "live client must not serve historical data"
    assert hasattr(TastytradeClient, "get_quotes"), "live quotes stay on Tastytrade"
    print("PASS: historical→Massive, live client retains quotes and has no get_candles")

    print("\nALL MASSIVE PROVIDER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
