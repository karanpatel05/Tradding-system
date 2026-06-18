"""Backtest↔live parity test — the headline guarantee.

Drives the SAME ``generate_signal`` through (a) the backtest ``replay`` loop and
(b) a hand-rolled "live" loop that feeds bars one at a time, and asserts the
resulting trades are identical. If the two ever diverge, the strategy logic is no
longer shared and this fails.

Run: python -m tests.test_ema_parity
"""

from __future__ import annotations

from app.strategies.ema_scalper.params import EmaScalperParams
from app.strategies.ema_scalper.signals import State, apply_action, generate_signal, replay
from tests.ema_fixtures import long_setup_bars


def _live_loop(bars, params, *, allow_long, allow_short, can_open, must_flat) -> list:
    """Simulate live: feed each freshly-closed bar to generate_signal once."""
    state = State()
    trades = []
    for i in range(params.warmup_bars, len(bars)):
        action = generate_signal(
            bars[: i + 1], state, params,
            allow_long=allow_long[i], allow_short=allow_short[i],
            can_open=can_open[i], must_flat=must_flat[i],
        )
        tr = apply_action(action, state, i, bars[i])
        if tr is not None:
            trades.append(tr)
    # mirror replay's end-of-data force-close
    if state.position is not None:
        last = bars[-1]
        from app.strategies.ema_scalper.signals import Action
        tr = apply_action(Action("exit", price=last["close"], reason="eod"),
                          state, len(bars) - 1, last)
        if tr is not None:
            trades.append(tr)
    return trades


def _run() -> None:
    p = EmaScalperParams.from_settings()
    bars = long_setup_bars()
    n = len(bars)
    flags = dict(allow_long=[True] * n, allow_short=[True] * n,
                 can_open=[True] * n, must_flat=[False] * n)

    bt = replay(bars, p, **flags)
    live = _live_loop(bars, p, **flags)

    assert len(bt) == len(live) == 1, (len(bt), len(live))
    for a, b in zip(bt, live):
        assert a == b, f"parity mismatch:\n backtest={a}\n live    ={b}"
        assert a.entry_type and a.entry_type == b.entry_type, (a.entry_type, b.entry_type)
    print(f"PASS: backtest and live identical (1 trade, entry_type={bt[0].entry_type})")

    # Same again with the regime blocking the direction → both produce nothing.
    blocked = dict(flags, allow_long=[False] * n)
    assert replay(bars, p, **blocked) == _live_loop(bars, p, **blocked) == []
    print("PASS: parity holds under the regime filter (both empty)")

    print("\nALL EMA PARITY TESTS PASSED")


if __name__ == "__main__":
    _run()
