"""Reconciliation compares only trades actually transmitted to Alpaca against the app's own
tagged-order ledger. Untagged (manual) account positions and Tastytrade are ignored.
Alpaca HTTP is mocked with httpx.MockTransport — no network."""
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, delete, inspect, text

from app.broker import alpaca_client as ac
from app.config import get_settings
from app.db.base import init_db, session_scope
from app.db.models import SystemFlag, TradeRecord
from app.engine import reconciliation as recon_mod
from app.engine import recovery as rec_mod
from app.engine.safe_mode import clear_safe_mode, is_safe_mode


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "PK")
    monkeypatch.setattr(s, "apca_api_secret_key", "sk")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")
    await init_db()
    await clear_safe_mode()
    async with session_scope() as sess:
        await sess.execute(delete(TradeRecord))
    yield
    async with session_scope() as sess:
        await sess.execute(delete(TradeRecord))
    await clear_safe_mode()


@pytest.fixture
def hooks(monkeypatch):
    """Capture alerts and backfill scheduling instead of running them."""
    seen = {"alerts": [], "backfill": 0}

    async def alert(kind, reason, meta=None):
        seen["alerts"].append((kind, reason))
    monkeypatch.setattr("app.services.alerts.send_alert", alert)
    monkeypatch.setattr(rec_mod, "_schedule_backfill",
                        lambda: seen.__setitem__("backfill", seen["backfill"] + 1))
    return seen


def _alpaca(monkeypatch, orders=(), positions=(), status=200):
    real = httpx.AsyncClient

    def handler(req: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"message": "internal error"})
        if req.url.path == "/v2/orders":
            return httpx.Response(200, json=list(orders))
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=list(positions))
        return httpx.Response(404, json={"message": "not found"})

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(ac.httpx, "AsyncClient", factory)


def _order(coid, sym, side, qty, filled, status="filled", **kw):
    return {"id": coid, "client_order_id": coid, "symbol": sym, "side": side,
            "qty": str(qty), "filled_qty": str(filled), "status": status, "legs": None, **kw}


def _pos(sym, qty, avg=100.0, side="long"):
    return {"symbol": sym, "qty": str(abs(qty)), "side": side,
            "avg_entry_price": str(avg), "asset_class": "us_equity"}


async def _trade(symbol="AAPL", side="buy", qty=10, transmitted=False, status="open"):
    async with session_scope() as sess:
        sess.add(TradeRecord(strategy_key="strategy_1", symbol=symbol, side=side,
                             quantity=qty, status=status, transmitted=transmitted))


async def _reason() -> str:
    async with session_scope() as sess:
        row = await sess.get(SystemFlag, "safe_mode")
        return (row.reason or "") if row else ""


# --- brief tests ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_simulated_open_trade_does_not_trip(monkeypatch, hooks):
    await _trade(transmitted=False)
    _alpaca(monkeypatch)                       # ledger {}
    assert await rec_mod.run_recovery() is True
    assert await is_safe_mode() is False


@pytest.mark.asyncio
async def test_transmitted_open_trade_matches_ledger(monkeypatch, hooks):
    await _trade(transmitted=True)
    _alpaca(monkeypatch, orders=[_order("tsys-a", "AAPL", "buy", 10, 10)],
            positions=[_pos("AAPL", 10)])
    assert await rec_mod.run_recovery() is True
    assert await is_safe_mode() is False
    assert hooks["backfill"] == 1              # clean recovery schedules backfill once


@pytest.mark.asyncio
async def test_untagged_account_positions_ignored(monkeypatch, hooks):
    _alpaca(monkeypatch, orders=[_order("manual-1", "TSLA", "buy", 100, 100)],
            positions=[_pos("TSLA", 100)])
    assert await rec_mod.run_recovery() is True
    assert await is_safe_mode() is False


@pytest.mark.asyncio
async def test_account_smaller_than_app_owned_trips(monkeypatch, hooks):
    await _trade(transmitted=True)
    _alpaca(monkeypatch, orders=[_order("tsys-a", "AAPL", "buy", 10, 10)],
            positions=[_pos("AAPL", 5)])
    assert await rec_mod.run_recovery() is False
    assert await is_safe_mode() is True
    assert "account 5 < app-owned 10" in await _reason()
    assert [k for k, _ in hooks["alerts"]] == ["safe_mode"]


@pytest.mark.asyncio
async def test_recovery_trips_with_broker_unavailable_reason(monkeypatch, hooks):
    _alpaca(monkeypatch, status=500)
    assert await rec_mod.run_recovery() is False
    assert await is_safe_mode() is True
    assert (await _reason()).startswith("broker unavailable")
    assert hooks["backfill"] == 0


@pytest.mark.asyncio
async def test_worker_marks_transmitted():
    from app.engine.worker import _open_trade
    tid_live = await _open_trade("strategy_1", "AAPL", "long", 10, 100.0, None,
                                 transmitted=True)
    tid_sim = await _open_trade("strategy_1", "AAPL", "long", 10, 100.0, None)
    async with session_scope() as sess:
        assert (await sess.get(TradeRecord, tid_live)).transmitted is True
        assert (await sess.get(TradeRecord, tid_sim)).transmitted is False


def test_migration_adds_transmitted_column(tmp_path):
    from app.db.base import _ensure_added_columns
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy_key VARCHAR(32), "
            "symbol VARCHAR(16), side VARCHAR(8), quantity FLOAT, status VARCHAR(8))"))
        conn.execute(text("INSERT INTO trades (strategy_key, symbol, side, quantity, status) "
                          "VALUES ('strategy_1', 'AAPL', 'buy', 10, 'open')"))
    with eng.begin() as conn:
        _ensure_added_columns(conn)
    with eng.begin() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("trades")}
        assert "transmitted" in cols
        assert conn.execute(text("SELECT transmitted FROM trades")).scalar_one() in (0, False)
        _ensure_added_columns(conn)            # idempotent
    eng.dispose()


# --- controller rulings / extra coverage ----------------------------------------------

@pytest.mark.asyncio
async def test_unconfigured_alpaca_returns_empty_snapshot(monkeypatch, hooks):
    s = get_settings()
    monkeypatch.setattr(s, "apca_api_key_id", "")
    monkeypatch.setattr(s, "apca_api_secret_key", "")

    def boom(*a, **kw):
        raise AssertionError("no HTTP when Alpaca is unconfigured")
    monkeypatch.setattr(ac.httpx, "AsyncClient", boom)
    snap = await recon_mod.fetch_snapshot()
    assert snap == {"positions": [], "fills": [], "orders": [], "issues": []}
    # nothing can have been transmitted, so a transmitted local trade is a discrepancy
    await _trade(transmitted=True)
    assert await rec_mod.run_recovery() is False
    assert "AAPL" in await _reason()


@pytest.mark.asyncio
async def test_option_symbols_compare_unpadded(monkeypatch, hooks):
    await _trade(symbol="QQQ   261117C00660000", qty=1, transmitted=True)
    _alpaca(monkeypatch, orders=[_order("tsys-o", "QQQ261117C00660000", "buy", 1, 1)],
            positions=[_pos("QQQ261117C00660000", 1, avg=2.5)])
    assert await rec_mod.local_open_positions() == {"QQQ261117C00660000": 1}
    assert await rec_mod.run_recovery() is True


@pytest.mark.asyncio
async def test_snapshot_shape_from_ledger(monkeypatch):
    open_parent = _order("tsys-p", "MSFT", "buy", 3, 0, status="new")
    _alpaca(monkeypatch,
            orders=[_order("tsys-a", "AAPL", "buy", 10, 10), open_parent,
                    _order("manual-1", "TSLA", "buy", 100, 100, status="new")],
            positions=[_pos("AAPL", 12, avg=101.5), _pos("TSLA", 100)])
    snap = await recon_mod.fetch_snapshot()
    assert snap["positions"] == [recon_mod.BrokerPosition("AAPL", 10, 101.5)]
    assert snap["fills"] == [] and snap["issues"] == []
    assert snap["orders"] == [recon_mod.BrokerOrder("MSFT", "tsys-p", "tsys-p", "new")]


@pytest.mark.asyncio
async def test_account_issue_cases(monkeypatch):
    _alpaca(monkeypatch,
            orders=[_order("tsys-a", "AAPL", "buy", 10, 10),     # account flat
                    _order("tsys-b", "MSFT", "sell", 4, 4),      # account long (opposite)
                    _order("tsys-c", "NVDA", "sell", 3, 3),      # account short larger: ok
                    _order("tsys-d", "AMD", "buy", 2, 2)],       # account larger: ok
            positions=[_pos("MSFT", 4), _pos("NVDA", 5, side="short"), _pos("AMD", 7)])
    issues = (await recon_mod.fetch_snapshot())["issues"]
    assert sorted(issues) == ["AAPL: account 0 < app-owned 10",
                              "MSFT: account 4 < app-owned -4"]


@pytest.mark.asyncio
async def test_closed_trades_not_counted():
    await _trade(transmitted=True, status="closed")
    await _trade(symbol="MSFT", side="sell", qty=3, transmitted=True)
    assert await rec_mod.local_open_positions() == {"MSFT": -3}


def test_reconcile_matrix():
    P = recon_mod.BrokerPosition
    reconcile = recon_mod.reconcile
    assert reconcile([], {}).ok is True
    assert reconcile([P("AAPL", 10, 100.0)], {"AAPL": 10}).ok is True
    r = reconcile([P("AAPL", 10, 100.0)], {})
    assert r.ok is False and any("AAPL" in d for d in r.discrepancies)
    assert reconcile([P("AAPL", 10, 100.0)], {"AAPL": 5}).ok is False
    assert reconcile([], {"AAPL": 10}).ok is False
