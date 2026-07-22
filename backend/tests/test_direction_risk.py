"""Live direction control + multi-position read APIs (slice 1 of P5).

Run: DATABASE_URL=sqlite+aiosqlite:///./_test_dir.db python -m tests.test_direction_risk

Covers: the additive ``direction`` migration on a pre-existing table, the
``direction_gates`` parity helper, the PUT /direction endpoint (+validation), and
the /positions and /risk read endpoints (risk reconstructed from the DB).
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.auth.gate import require_auth
from app.main import app
from app.strategies.ema_crossover.signals import direction_gates


def _test_direction_gates() -> None:
    # direction AND HTF, the single shared gate for live + backtest.
    assert direction_gates("both", True, True) == (True, True)
    assert direction_gates("long", True, True) == (True, False)
    assert direction_gates("short", True, True) == (False, True)
    assert direction_gates("long", False, True) == (False, False)   # HTF vetoes long
    assert direction_gates("both", True, False) == (True, False)    # HTF vetoes short
    print("PASS: direction_gates combines direction × HTF")


def _test_migration() -> None:
    """A strategy_state table created WITHOUT `direction` gets the column added."""
    import sqlite3
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE strategy_state (key VARCHAR PRIMARY KEY, name VARCHAR, "
                  "ready BOOLEAN, status VARCHAR, selected_symbol VARCHAR, "
                  "capital_allocation FLOAT, updated_at DATETIME)")
        c.execute("INSERT INTO strategy_state (key, name) VALUES ('strategy_1', 'X')")
        c.commit(); c.close()

        async def _mig():
            from app.db.base import init_db, engine
            import app.db.base as base
            from sqlalchemy.ext.asyncio import create_async_engine
            # Point a fresh engine at the legacy DB and run the additive migration.
            base.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            try:
                await init_db()
            finally:
                await base.engine.dispose()
                base.engine = engine
        asyncio.run(_mig())

        c = sqlite3.connect(path)
        cols = [r[1] for r in c.execute("PRAGMA table_info(strategy_state)")]
        val = c.execute("SELECT direction FROM strategy_state WHERE key='strategy_1'").fetchone()[0]
        c.close()
        assert "direction" in cols, cols
        assert val == "both", val
        print("PASS: additive migration adds strategy_state.direction (default both)")
    finally:
        os.unlink(path)


def _test_endpoints() -> None:
    with TestClient(app) as client:  # lifespan: init_db + seed
        key = "strategy_1"

        # default direction is both
        d = client.get(f"/api/strategies/{key}").json()
        assert d["direction"] == "both", d.get("direction")

        # set + validate
        r = client.put(f"/api/strategies/{key}/direction", json={"direction": "short"})
        assert r.status_code == 200 and r.json()["direction"] == "short", r.text
        assert client.get(f"/api/strategies/{key}").json()["direction"] == "short"
        bad = client.put(f"/api/strategies/{key}/direction", json={"direction": "sideways"})
        assert bad.status_code == 422, bad.status_code
        # Coming-Soon strategy is gated
        assert client.put("/api/strategies/strategy_2/direction",
                          json={"direction": "long"}).status_code == 409
        print("PASS: PUT /direction sets/validates/gates")

        # positions: empty, then with an injected open trade
        empty = client.get(f"/api/strategies/{key}/positions").json()
        assert empty == {"positions": [], "count": 0}, empty

        async def _add_trade():
            from datetime import datetime, timezone
            from app.db.base import session_scope
            from app.db.models import TradeRecord
            async with session_scope() as s:
                s.add(TradeRecord(strategy_key=key, symbol="NVDA", side="buy", quantity=10,
                                  entry_price=100.0, stop=98.0, target=104.0, status="open",
                                  entry_time=datetime.now(timezone.utc)))
        asyncio.run(_add_trade())

        pos = client.get(f"/api/strategies/{key}/positions").json()
        assert pos["count"] == 1 and pos["positions"][0]["symbol"] == "NVDA", pos
        assert pos["positions"][0]["side"] == "long" and pos["positions"][0]["notional"] == 1000.0
        print("PASS: GET /positions lists all open positions")

        # set an allocation so the risk snapshot has equity, then check /risk
        client.put(f"/api/strategies/{key}/allocation", json={"amount": 10000})
        rk = client.get(f"/api/strategies/{key}/risk").json()
        for k in ("equity", "open_positions", "used_cash", "used_risk", "trades_today",
                  "realized_today", "halted", "direction", "running"):
            assert k in rk, (k, rk.keys())
        assert rk["equity"] == 10000.0 and rk["open_positions"] == 1, rk
        assert rk["used_cash"] == 1000.0 and rk["used_risk"] == 20.0, rk  # 10*100, 10*|100-98|
        assert rk["trades_today"] == 1 and rk["direction"] == "short", rk
        print("PASS: GET /risk reconstructs the engine snapshot from the DB")

    print("\nALL DIRECTION/RISK TESTS PASSED")


def _run() -> None:
    # Bypass the caller-auth gate for this module-runner's endpoint checks. Set here
    # (not at import) so importing the module under pytest doesn't leak the override
    # into the shared app singleton and disable auth for the whole session.
    app.dependency_overrides[require_auth] = lambda: None
    _test_direction_gates()
    _test_migration()
    _test_endpoints()


if __name__ == "__main__":
    _run()
