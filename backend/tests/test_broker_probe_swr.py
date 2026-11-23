"""probe() never makes hot paths wait on a live Tastytrade refresh: stale cache is
served immediately with one background refresh; only a cold call waits, bounded."""
import asyncio
import time

import pytest

from app.broker import status as st


def _counting_probe(gate: asyncio.Event | None = None, result="ok"):
    calls: list[bool] = []

    async def fake(force):
        calls.append(force)
        if gate is not None:
            await gate.wait()
        return result
    return fake, calls


async def _hang(force):
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_concurrent_stale_calls_return_cached_and_refresh_once(monkeypatch):
    gate = asyncio.Event()
    fake, calls = _counting_probe(gate, result="rejected: new")
    monkeypatch.setattr(st, "_do_probe", fake)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic() - 10_000})

    t0 = time.monotonic()
    results = await asyncio.wait_for(
        asyncio.gather(*(st.probe() for _ in range(10))), 1.0)
    assert time.monotonic() - t0 < 0.5
    assert results == ["ok"] * 10           # stale value served, no waiting
    await asyncio.sleep(0)                   # let the background task start
    assert calls == [False]                  # exactly one refresh in flight

    gate.set()
    await st._inflight
    assert calls == [False]
    assert st._cache["result"] == "rejected: new"
    assert await st.probe() == "rejected: new"   # now fresh -> no new refresh
    assert calls == [False]


@pytest.mark.asyncio
async def test_hanging_refresh_never_blocks_status_endpoint(monkeypatch):
    from app.auth import routes
    from app.auth.routes import _status

    async def _no_safe_mode():
        return False, None
    monkeypatch.setattr(routes, "_safe_mode_info", _no_safe_mode)  # isolate the probe
    monkeypatch.setattr(st, "_do_probe", _hang)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic() - 10_000})

    for _ in range(3):
        status = await asyncio.wait_for(_status(), 1.0)
        assert status.connected is True
    assert st._inflight is not None and not st._inflight.done()


@pytest.mark.asyncio
async def test_status_route_over_http_with_hanging_refresh(monkeypatch):
    import httpx

    from app.main import app
    monkeypatch.setattr(st, "_do_probe", _hang)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic() - 10_000})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # The refresh hangs forever, so any finite bound proves it isn't awaited;
        # kept loose because the first DB connect can be slow on a loaded machine.
        r = await asyncio.wait_for(c.get("/api/auth/status"), 10.0)
    assert r.status_code == 200
    assert r.json()["connected"] is True


@pytest.mark.asyncio
async def test_cold_hanging_probe_is_bounded_then_served_from_cache(monkeypatch):
    monkeypatch.setattr(st, "_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(st, "_do_probe", _hang)
    monkeypatch.setattr(st, "_cache", {"result": None, "at": 0.0})

    t0 = time.monotonic()
    assert await st.probe() == st.TIMEOUT_RESULT
    assert time.monotonic() - t0 < 1.0
    first = st._inflight
    # Next poll: immediate, same single refresh still running (not cancelled).
    assert await asyncio.wait_for(st.probe(), 0.02) == st.TIMEOUT_RESULT
    assert st._inflight is first and not first.done()
    assert st.is_ready_cached() is False


@pytest.mark.asyncio
async def test_timed_out_cold_probe_result_lands_later(monkeypatch):
    gate = asyncio.Event()
    fake, calls = _counting_probe(gate)
    monkeypatch.setattr(st, "_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(st, "_do_probe", fake)
    monkeypatch.setattr(st, "_cache", {"result": None, "at": 0.0})

    assert await st.probe() == st.TIMEOUT_RESULT
    gate.set()
    await st._inflight
    assert st.is_ready_cached() is True
    assert calls == [False]


@pytest.mark.asyncio
async def test_refresh_itself_is_bounded(monkeypatch):
    monkeypatch.setattr(st, "_PROBE_TIMEOUT", 0.05)
    monkeypatch.setattr(st, "_do_probe", _hang)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic() - 10_000})
    await st.probe()
    assert await asyncio.wait_for(st._inflight, 1.0) == st.TIMEOUT_RESULT
    assert st._cache["result"] == st.TIMEOUT_RESULT
    # A timeout result counts as stale: the next call schedules a fresh retry.
    old = st._inflight
    await st.probe()
    assert st._inflight is not old


@pytest.mark.asyncio
async def test_force_probes_even_when_fresh(monkeypatch):
    fake, calls = _counting_probe(result="rejected: invalid_grant")
    monkeypatch.setattr(st, "_do_probe", fake)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic()})
    assert await st.probe() == "ok" and calls == []
    assert await st.probe(force=True) == "rejected: invalid_grant"
    assert calls == [True]
    assert st.is_ready_cached() is False


@pytest.mark.asyncio
async def test_concurrent_forces_share_one_probe(monkeypatch):
    gate = asyncio.Event()
    fake, calls = _counting_probe(gate)
    monkeypatch.setattr(st, "_do_probe", fake)
    monkeypatch.setattr(st, "_cache", {"result": None, "at": 0.0})
    pending = asyncio.gather(st.probe(force=True), st.probe(force=True))
    await asyncio.sleep(0.01)
    gate.set()
    assert await pending == ["ok", "ok"]
    assert calls == [True]


@pytest.mark.asyncio
async def test_force_supersedes_background_refresh(monkeypatch):
    """A forced probe (e.g. after saving new creds) doesn't reuse a background refresh
    that started earlier, and that older refresh can't overwrite the newer result."""
    old_gate = asyncio.Event()
    seen: list[bool] = []

    async def fake(force):
        seen.append(force)
        if not force:
            await old_gate.wait()
            return "rejected: old creds"
        return "ok"
    monkeypatch.setattr(st, "_do_probe", fake)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic() - 10_000})

    await st.probe()                       # schedules the background refresh
    bg = st._inflight
    await asyncio.sleep(0)
    assert await st.probe(force=True) == "ok"
    assert seen == [False, True]
    assert bg in st._tasks and st._inflight is not bg   # superseded, still referenced
    old_gate.set()
    await bg
    assert st._cache["result"] == "ok"     # stale background result discarded


@pytest.mark.asyncio
async def test_force_timeout_keeps_existing_cache(monkeypatch):
    monkeypatch.setattr(st, "_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(st, "_do_probe", _hang)
    monkeypatch.setattr(st, "_cache", {"result": "ok", "at": time.monotonic()})
    assert await st.probe(force=True) == st.TIMEOUT_RESULT
    assert st.is_ready_cached() is True
