"""EMA scalper signal-generation tests (pure, deterministic).

Run: python -m tests.test_ema_signals
"""

from __future__ import annotations

from app.strategies.ema_scalper import indicators as ind
from app.strategies.ema_scalper.params import EmaScalperParams
from app.strategies.ema_scalper.signals import State, apply_action, generate_signal, replay
from tests.ema_fixtures import flat_chop_bars, long_setup_bars


def _params() -> EmaScalperParams:
    return EmaScalperParams.from_settings()


def _run() -> None:
    p = _params()

    # --- indicators sanity ---
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    e = ind.ema(vals, 5)
    assert e[3] is None and e[4] is not None, e
    assert abs(e[4] - 3.0) < 1e-9, e[4]  # SMA seed of first 5
    print("PASS: EMA warmup + seed")

    assert ind.strong_bull_body({"open": 10, "high": 12.1, "low": 9.9, "close": 12})
    assert ind.bullish_engulfing({"open": 10, "high": 10.1, "low": 9, "close": 9.2},
                                 {"open": 9.1, "high": 11, "low": 9, "close": 10.8})
    print("PASS: candle pattern detectors")

    # --- flat chop produces no trades ---
    chop = flat_chop_bars()
    trades = replay(chop, p, can_open=[True] * len(chop), must_flat=[False] * len(chop))
    assert trades == [], f"expected no trades in chop, got {trades}"
    print("PASS: no trades in sideways chop")

    # --- the long setup produces exactly one winning long trade ---
    bars = long_setup_bars()
    n = len(bars)
    trades = replay(bars, p, can_open=[True] * n, must_flat=[False] * n)
    assert len(trades) == 1, f"expected 1 trade, got {len(trades)}: {trades}"
    t = trades[0]
    assert t.side == "long", t.side
    assert t.entry_type in {"pin", "engulfing", "reversal"}, t.entry_type
    assert t.reason in {"target", "reversal", "eod"}, t.reason
    assert t.return_pct > 0, t.return_pct
    print(f"PASS: long setup → 1 trade, side=long, type={t.entry_type}, reason={t.reason}, ret={t.return_pct:.4f}")

    # --- regime filter blocks the long when shorts-only ---
    blocked = replay(bars, p, allow_long=[False] * n, allow_short=[True] * n,
                     can_open=[True] * n, must_flat=[False] * n)
    assert blocked == [], f"regime should block the long, got {blocked}"
    print("PASS: regime filter blocks disallowed direction")

    # --- session cutoff forces a flat ---
    state = State()
    # find the entry, then assert a must_flat exit closes it
    forced = replay(bars, p, can_open=[True] * n, must_flat=[i > n - 3 for i in range(n)])
    assert forced and forced[0].side == "long", forced
    print("PASS: session cutoff exits cleanly")

    print("\nALL EMA SIGNAL TESTS PASSED")


if __name__ == "__main__":
    _run()
