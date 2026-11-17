import pytest
from sqlalchemy import select

from app.db.base import init_db, session_scope
from app.db.models import OptionPositionRecord, StrategyState
from app.strategies import registry
from app.strategies.base import ComingSoonStrategy


def test_registry_has_no_triple_calendar():
    s2 = registry.get_strategy("strategy_2")
    assert isinstance(s2, ComingSoonStrategy)
    assert s2.ready is False
    assert s2.name == "Strategy 2"
    assert all(type(s).__name__ != "TripleCalendarStrategy" for s in registry.list_strategies())


@pytest.mark.asyncio
async def test_seed_resets_non_ready_slot_status():
    await init_db()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        if st is None:
            st = StrategyState(key="strategy_2", name="Triple Calendar", ready=True,
                               status="scanning")
            s.add(st)
        else:
            st.name, st.ready, st.status = "Triple Calendar", True, "scanning"
    await registry.seed_strategy_state()
    async with session_scope() as s:
        st = await s.get(StrategyState, "strategy_2")
        assert st.ready is False
        assert st.name == "Strategy 2"
        assert st.status == "idle"


@pytest.mark.asyncio
async def test_close_stale_tc_positions_idempotent():
    from app.db.base import close_stale_tc_positions

    await init_db()
    async with session_scope() as s:
        # isolate from rows left by other tests in a shared DB
        for r in (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key.in_(["strategy_2", "other_key"])))).scalars():
            await s.delete(r)
    async with session_scope() as s:
        a = OptionPositionRecord(strategy_key="strategy_2", underlying="QQQ", status="open",
                                 calendars=[], order_ids=[])
        b = OptionPositionRecord(strategy_key="strategy_2", underlying="SPY", status="open",
                                 calendars=[], order_ids=[])
        c = OptionPositionRecord(strategy_key="other_key", underlying="IWM", status="open",
                                 calendars=[], order_ids=[])
        s.add_all([a, b, c])
        await s.flush()
        ids = (a.id, b.id, c.id)
    async with session_scope() as s:
        assert await close_stale_tc_positions(s) == 2
    async with session_scope() as s:
        for i in ids[:2]:
            r = await s.get(OptionPositionRecord, i)
            assert r.status == "closed"
            assert r.pnl is None
            assert r.exit_time is not None
        other = await s.get(OptionPositionRecord, ids[2])
        assert other.status == "open"
        assert other.exit_time is None
    async with session_scope() as s:
        assert await close_stale_tc_positions(s) == 0
        other = await s.get(OptionPositionRecord, ids[2])
        await s.delete(other)


def test_builder_chain_imports_live_source():
    import app.options.chain as chain
    import app.options.live_source as ls

    assert chain.LivePriceSource is ls.LivePriceSource


@pytest.mark.asyncio
async def test_init_db_closes_stale_tc_rows_only_once():
    """The stale-TC cleanup is a one-shot data fix guarded by a SystemFlag: it runs on the
    first boot after the removal, then never again — slot 2 is reusable, so a future
    options strategy's open rows there must survive every later restart."""
    from app.db.models import SystemFlag

    await init_db()
    async with session_scope() as s:
        for r in (await s.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == "strategy_2"))).scalars():
            await s.delete(r)
        flag = await s.get(SystemFlag, "tc_stale_rows_closed")
        if flag is not None:
            await s.delete(flag)
        stale = OptionPositionRecord(strategy_key="strategy_2", underlying="QQQ",
                                     status="open", calendars=[], order_ids=[])
        s.add(stale)
        await s.flush()
        stale_id = stale.id
    await init_db()                                   # first boot after the removal
    async with session_scope() as s:
        assert (await s.get(OptionPositionRecord, stale_id)).status == "closed"
        flag = await s.get(SystemFlag, "tc_stale_rows_closed")
        assert flag is not None and flag.on is True
        fresh = OptionPositionRecord(strategy_key="strategy_2", underlying="SPY",
                                     status="open", calendars=[], order_ids=[])
        s.add(fresh)
        await s.flush()
        fresh_id = fresh.id
    await init_db()                                   # a later restart
    async with session_scope() as s:
        row = await s.get(OptionPositionRecord, fresh_id)
        assert row.status == "open" and row.exit_time is None
        await s.delete(row)
        await s.delete(await s.get(OptionPositionRecord, stale_id))
