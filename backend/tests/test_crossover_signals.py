"""9/15 EMA crossover signal tests (pure, deterministic).

Run: python -m tests.test_crossover_signals

Covers: bullish + bearish crossover entries, exits, the volume gate, the HTF
(allow_long/allow_short) gate, no-trade chop, and backtest/live PARITY.
"""

from __future__ import annotations

from dataclasses import replace

from app.strategies.ema_crossover.params import EmaCrossoverParams
from app.strategies.ema_crossover.signals import (
    Action, State, apply_action, generate_signal, replay,
)

_T0 = 1_700_000_000_000
_STEP = 300_000  # 5 minutes in ms


def _bar(i, c, v):
    return {"time": _T0 + i * _STEP, "open": c, "high": c + 0.3, "low": c - 0.3,
            "close": c, "volume": v}


def _params() -> EmaCrossoverParams:
    # These tests exercise session-agnostic signal mechanics on synthetic (out-of-RTH)
    # timestamps, so disable the RTH gate — session timing is covered separately in
    # test_crossover_session / test_crossover_rth_wired.
    return replace(EmaCrossoverParams.from_settings(), use_rth_filter=False)


def long_setup() -> list[dict]:
    """25 declining bars (EMA9<EMA15) then 20 sharply rising bars + volume spike."""
    bars = [_bar(k, 110.0 - k * 0.4, 1000) for k in range(25)]      # 110 → 100.4
    base = bars[-1]["close"]
    bars += [_bar(25 + k, base + (k + 1) * 0.9, 5000) for k in range(20)]  # sharp rise, 5× vol
    return bars


def short_setup() -> list[dict]:
    """25 rising bars then 20 sharply falling bars + volume spike."""
    bars = [_bar(k, 100.0 + k * 0.4, 1000) for k in range(25)]
    base = bars[-1]["close"]
    bars += [_bar(25 + k, base - (k + 1) * 0.9, 5000) for k in range(20)]
    return bars


def flat_chop() -> list[dict]:
    return [_bar(k, 100.0 + (0.1 if k % 2 else -0.1), 1000) for k in range(45)]


def _run() -> None:
    p = _params()

    # --- bullish crossover entry ---
    bars = long_setup()
    trades = replay(bars, p, symbol="TEST")
    assert trades, "expected a long crossover trade"
    t = trades[0]
    assert t.side == "long", t.side
    assert t.stop < t.entry_price < t.target, (t.stop, t.entry_price, t.target)
    assert t.reason in {"target", "crossover", "end"}, t.reason
    print(f"PASS: bullish crossover → long (entry {t.entry_price:.2f}, exit {t.exit_price:.2f}, {t.reason})")

    # --- bearish crossover entry ---
    strades = replay(short_setup(), p, symbol="TEST")
    assert strades and strades[0].side == "short", strades
    s = strades[0]
    assert s.target < s.entry_price < s.stop, (s.target, s.entry_price, s.stop)
    print(f"PASS: bearish crossover → short (entry {s.entry_price:.2f}, exit {s.exit_price:.2f}, {s.reason})")

    # --- no signal in flat chop ---
    assert replay(flat_chop(), p) == [], "flat chop should produce no trades"
    print("PASS: no trades in flat chop")

    # --- volume gate: same setup but no volume surge → no entry ---
    no_vol = [{**b, "volume": 1000} for b in long_setup()]  # phase-2 volume back to baseline
    assert replay(no_vol, p) == [], "entry must require volume > multiplier × avg"
    print("PASS: volume gate blocks entry without a volume surge")

    # --- HTF gate: block longs ---
    n = len(bars)
    blocked = replay(bars, p, allow_long=[False] * n, allow_short=[True] * n)
    assert blocked == [], f"allow_long=False must block the long, got {blocked}"
    print("PASS: HTF/regime gate blocks the disallowed direction")

    # --- PARITY: replay == per-bar generate_signal loop ---
    state = State()
    live: list = []
    for i in range(1, n):
        act = generate_signal(bars[: i + 1], state, p)
        if act.kind != "none":
            tr = apply_action(act, state, i, bars[i], "TEST")
            if tr:
                live.append(tr)
    if state.position is not None:  # mirror replay's end-of-data close
        last = bars[-1]
        tr = apply_action(Action("exit", state.position.side, last["close"], reason="end"),
                          state, n - 1, last, "TEST")
        if tr:
            live.append(tr)

    def key(tr):
        return (tr.side, tr.entry_index, tr.exit_index, round(tr.entry_price, 6),
                round(tr.exit_price, 6), tr.reason)

    assert [key(x) for x in live] == [key(x) for x in trades], (
        [key(x) for x in live], [key(x) for x in trades])
    print(f"PASS: backtest/live parity ({len(trades)} trade(s) identical)")

    print("\nALL CROSSOVER SIGNAL TESTS PASSED")


if __name__ == "__main__":
    _run()
