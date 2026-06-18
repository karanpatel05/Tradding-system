"""Server-side Coming-Soon enforcement test.

Auth is bypassed via a dependency override so we prove the *business* gate (409
for not-ready strategies) is enforced in the backend, independent of any UI.

Run: DATABASE_URL=sqlite+aiosqlite:///./_test_strat.db python -m tests.test_strategies
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth.gate import require_auth
from app.main import app

app.dependency_overrides[require_auth] = lambda: None  # bypass auth for the test


def _run() -> None:
    with TestClient(app) as client:  # triggers lifespan: init_db + seed
        # 5 strategies: strategy_1 is the live EMA scalper, the rest Coming Soon.
        r = client.get("/api/strategies")
        assert r.status_code == 200, r.text
        strategies = {s["key"]: s for s in r.json()["strategies"]}
        assert len(strategies) == 5, strategies
        assert strategies["strategy_1"]["ready"] is True, strategies["strategy_1"]
        assert all(strategies[f"strategy_{i}"]["ready"] is False for i in range(2, 6))
        print("PASS: strategy_1 live, strategies 2-5 Coming Soon")

        # strategy_1 overview exposes the real rules (not a Coming-Soon shell).
        ov = client.get("/api/strategies/strategy_1").json()["overview"]
        assert ov["ready"] is True and ov.get("rules"), ov
        print("PASS: strategy_1 overview exposes rules")

        # A Coming-Soon strategy is still gated server-side (409).
        r = client.post("/api/strategies/strategy_2/activate")
        assert r.status_code == 409, (r.status_code, r.text)
        r = client.put("/api/strategies/strategy_2/allocation", json={"amount": 1000})
        assert r.status_code == 409, (r.status_code, r.text)
        print("PASS: Coming-Soon strategy_2 activate/allocate -> 409")

        # The live strategy accepts an allocation (the Coming-Soon gate doesn't fire).
        r = client.put("/api/strategies/strategy_1/allocation", json={"amount": 5000})
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json()["capital_allocation"] == 5000
        print("PASS: strategy_1 accepts allocation")

        # Unknown strategy -> 404.
        r = client.post("/api/strategies/strategy_99/activate")
        assert r.status_code == 404, r.status_code
        print("PASS: unknown strategy -> 404")

    print("\nALL STRATEGY ENFORCEMENT TESTS PASSED")


if __name__ == "__main__":
    _run()
