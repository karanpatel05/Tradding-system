"""Probe-backed readiness: cached result, force refresh, sync read for gates."""
import pytest

from app.broker import status as st


@pytest.mark.asyncio
async def test_probe_cached_and_forced(monkeypatch):
    calls = []
    async def fake_refresh(force):
        calls.append(force)
        return "ok"
    monkeypatch.setattr(st, "_do_probe", fake_refresh)
    st._cache.update(result=None, at=0.0)
    assert await st.probe() == "ok"          # cold -> probes
    assert await st.probe() == "ok"          # warm -> cached (no second call)
    assert calls == [False]
    assert await st.probe(force=True) == "ok"
    assert calls == [False, True]
    assert st.is_ready_cached() is True


@pytest.mark.asyncio
async def test_rejected_probe_not_ready(monkeypatch):
    async def bad(force):
        return "rejected: invalid_grant"
    monkeypatch.setattr(st, "_do_probe", bad)
    st._cache.update(result=None, at=0.0)
    await st.probe()
    assert st.is_ready_cached() is False
