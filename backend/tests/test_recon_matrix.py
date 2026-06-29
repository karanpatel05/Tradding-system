"""Reconciliation matrix: each broker×local cell -> ok or discrepancy.
   Run: python -m tests.test_recon_matrix
"""
from __future__ import annotations


def _run() -> None:
    from app.engine.reconciliation import BrokerPosition, reconcile

    P = BrokerPosition
    # broker flat + local flat
    assert reconcile([], {}).ok is True
    # broker matches local
    assert reconcile([P("AAPL", 10, 100.0)], {"AAPL": 10}).ok is True
    # broker has unknown position, local flat -> NOT ok
    r = reconcile([P("AAPL", 10, 100.0)], {})
    assert r.ok is False and any("AAPL" in d for d in r.discrepancies), r
    # qty mismatch -> NOT ok
    assert reconcile([P("AAPL", 10, 100.0)], {"AAPL": 5}).ok is False
    # local thinks open, broker flat -> NOT ok (could be a missed close; caller decides)
    assert reconcile([], {"AAPL": 10}).ok is False
    print("PASS: matrix cells evaluated")
    print("\nALL RECON-MATRIX TESTS PASSED")


if __name__ == "__main__":
    _run()
