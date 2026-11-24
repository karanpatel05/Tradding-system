import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.history.alpaca_bars import AlpacaDataError, RateLimiter, fetch_minutes

START = datetime(2022, 1, 3, 14, 30, tzinfo=timezone.utc)
END = datetime(2022, 1, 4, 21, 0, tzinfo=timezone.utc)


def _bar(t, o=1.0):
    return {"t": t, "o": o, "h": o + 1, "l": o - 1, "c": o + 0.5, "v": 100}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_pages_are_followed_and_mapped():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if "page_token" not in req.url.params:
            return httpx.Response(200, json={
                "bars": {"SPY": [_bar("2022-01-03T14:30:00Z"), _bar("2022-01-03T14:31:00Z")]},
                "next_page_token": "p2"})
        return httpx.Response(200, json={
            "bars": {"SPY": [_bar("2022-01-03T14:32:00Z"), {"t": "2022-01-03T14:33:00Z", "o": 1}]},
            "next_page_token": None})

    async def go():
        async with _client(handler) as c:
            return await fetch_minutes("SPY", START, END, client=c,
                                       limiter=RateLimiter(per_minute=100000))

    bars = asyncio.run(go())
    assert len(bars) == 3  # malformed bar skipped
    assert bars[0] == {"time": 1641220200000, "open": 1.0, "high": 2.0,
                       "low": 0.0, "close": 1.5, "volume": 100}
    assert [b["time"] for b in bars] == sorted(b["time"] for b in bars)
    assert seen[1]["page_token"] == "p2"
    for p in seen:
        assert p["feed"] == "sip" and p["adjustment"] == "split"
        assert p["timeframe"] == "1Min" and p["limit"] == "10000"
        assert p["symbols"] == "SPY"


def test_errors_raise_with_status():
    async def go(resp):
        async with _client(lambda r: resp) as c:
            await fetch_minutes("SPY", START, END, client=c)

    with pytest.raises(AlpacaDataError) as e:
        asyncio.run(go(httpx.Response(401, json={"message": "forbidden"})))
    assert e.value.status == 401 and e.value.recent_sip_denied is False

    msg = {"message": "subscription does not permit querying recent SIP data"}
    with pytest.raises(AlpacaDataError) as e:
        asyncio.run(go(httpx.Response(403, content=json.dumps(msg))))
    assert e.value.status == 403 and e.value.recent_sip_denied is True


def test_limiter_spaces_calls():
    async def go():
        lim = RateLimiter(per_minute=600)
        t0 = time.monotonic()
        for _ in range(3):
            await lim.acquire()
        return time.monotonic() - t0

    assert asyncio.run(go()) >= 0.19
