"""Event-driven portfolio backtest for the multi-position EMA crossover.

This is the backtest counterpart of the live ``_run_multi_position`` worker, and it
achieves **sizing parity by construction**: it drives the *same*
``RiskEngine.check_and_size`` the live worker uses, over a single shared
buying-power pool, so historical share quantities match what live would have done.
(Signal parity is already guaranteed by funnelling every decision through
``signals.decide_index``.)

How it stays faithful to live:

* **Merged timeline.** All symbols' bars are merged and processed in
  non-decreasing timestamp order; ties break on watchlist position, so within one
  instant symbols are handled in the same order the live loop iterates them — an
  earlier symbol's exit frees pool for a later symbol's entry at the same instant.
* **``now`` threaded everywhere.** Each ``check_and_size`` / ``register_open`` /
  ``register_close`` is passed the *bar's* timestamp as ``now`` (and the engine is
  seeded with the first bar's ET date). Without this the engine's daily counters
  would use wall-clock time — "today" would never advance across history and the
  per-day trade cap / 3% daily-loss halt would latch forever. Threading ``now``
  is what makes those limits model real historical ET days.
* **State/engine sync mirrors ``_handle_mp_action``.** ``state.position`` is set
  only when an entry is admitted (``quantity > 0``); a rejected entry leaves the
  symbol flat. Exits clear the position and book P&L with the live formula
  ``(exit − entry) × qty × (+1 long / −1 short)``.

Equity is constant (= the strategy's allocated capital), matching live where
``engine.equity`` is the static allocation; realized P&L accumulates per ET day for
the daily-loss halt and for metrics, and is **not** compounded into equity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.engine.risk import RiskEngine, RiskLimits
from app.strategies.ema_crossover.params import EmaCrossoverParams
from app.strategies.ema_crossover.signals import (
    Action, Series, State, Trade, apply_action, decide_index, direction_gates,
    resolve_intrabar_exit,
)

_ET = ZoneInfo("America/New_York")
_5M_MS = 300_000


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _et_date_ms(ms: int) -> date:
    return _utc(ms).astimezone(_ET).date()


def _group_1m_by_5m(bars_5m: list[dict], bars_1m: list[dict]) -> dict[int, list[dict]]:
    """Map each 5m bar's start time → the 1m bars inside its [t, t+5m) window.

    Alignment-agnostic (keyed on each 5m bar's own start time, half-open window), so
    it's correct whether Polygon aligns 5m to the epoch or the session. Both inputs
    must be ascending by time. A window with no 1m bars maps to ``[]`` — the caller
    then falls back to the 5m stop/target so stops are never silently skipped."""
    out: dict[int, list[dict]] = {}
    j, n = 0, len(bars_1m)
    for b5 in bars_5m:
        t0 = b5.get("time") or 0
        t1 = t0 + _5M_MS
        while j < n and (bars_1m[j].get("time") or 0) < t0:
            j += 1
        k = j
        bucket: list[dict] = []
        while k < n and (bars_1m[k].get("time") or 0) < t1:
            bucket.append(bars_1m[k])
            k += 1
        out[t0] = bucket
        j = k
    return out


@dataclass
class PortfolioTrade:
    """A realized, risk-sized trade from the portfolio sim. Wraps the pure
    ``signals.Trade`` (which owns side/prices/return_pct/r_multiple) and adds the
    actual share quantity the risk engine sized, from which $-P&L follows."""

    trade: Trade
    shares: int

    @property
    def _dir(self) -> int:
        return 1 if self.trade.side == "long" else -1

    @property
    def pnl(self) -> float:
        return (self.trade.exit_price - self.trade.entry_price) * self.shares * self._dir

    @property
    def capital_used(self) -> float:
        return self.shares * self.trade.entry_price

    @property
    def hold_minutes(self) -> float | None:
        a, b = self.trade.entry_time, self.trade.exit_time
        if a is None or b is None:
            return None
        return (b - a) / 60_000.0


def simulate_portfolio(bars_by_symbol: dict[str, list[dict]], params: EmaCrossoverParams,
                       limits: RiskLimits, equity: float, *, direction: str = "both",
                       htf_by_symbol: dict[str, tuple[list[bool], list[bool]]] | None = None,
                       watchlist_order: list[str] | None = None,
                       bars_1m_by_symbol: dict[str, list[dict]] | None = None,
                       windows_by_symbol: dict[str, dict[int, list[dict]]] | None = None) -> list[PortfolioTrade]:
    """Run the multi-position crossover over historical bars and return realized,
    risk-sized trades in exit order.

    ``direction`` is ``"long"`` / ``"short"`` / ``"both"`` and AND-combines with the
    optional per-symbol HTF gate (``htf_by_symbol[symbol] = (allow_long[], allow_short[])``
    aligned to that symbol's bars).

    ``bars_1m_by_symbol`` (optional) enables **1-minute intrabar fill resolution**:
    while a position is open, its stop/target are resolved by walking that 5m
    window's 1m bars (``resolve_intrabar_exit``) — the higher-fidelity model that
    matches a live broker OTOCO. Per window: 1m when present, else fall back to the
    5m stop/target (so a 1m data gap never silently skips a stop). Omit it entirely
    and the sim is byte-for-byte the original 5m behavior."""
    order_src = watchlist_order or list(bars_by_symbol.keys())
    symbols = [s for s in order_src if bars_by_symbol.get(s)]
    order = {s: idx for idx, s in enumerate(symbols)}

    series = {s: Series(bars_by_symbol[s], params) for s in symbols}
    states = {s: State() for s in symbols}
    open_shares: dict[str, int] = {}
    windows: dict[str, dict[int, list[dict]]] = {}
    if bars_1m_by_symbol:
        for s in symbols:
            if bars_1m_by_symbol.get(s):
                windows[s] = _group_1m_by_5m(bars_by_symbol[s], bars_1m_by_symbol[s])

    # Merge every symbol's bars into one timeline, ordered by (time, watchlist pos).
    events: list[tuple[int, int, str, int]] = []
    for s in symbols:
        bars = bars_by_symbol[s]
        for i in range(1, len(bars)):
            events.append((bars[i].get("time") or 0, order[s], s, i))
    events.sort()
    if not events:
        return []

    engine = RiskEngine(equity, limits, today=_et_date_ms(events[0][0]))
    trades: list[PortfolioTrade] = []

    def _gates(s: str, i: int) -> tuple[bool, bool]:
        htf_long, htf_short = True, True
        if htf_by_symbol and s in htf_by_symbol:
            hl, hs = htf_by_symbol[s]
            htf_long = hl[i] if i < len(hl) else True
            htf_short = hs[i] if i < len(hs) else True
        return direction_gates(direction, htf_long, htf_short)

    def _close(s: str, action: Action, i: int, bar: dict, now: datetime) -> None:
        shares = open_shares.pop(s, 0)
        tr = apply_action(action, states[s], i, bar, s)
        if tr is None:
            return
        pt = PortfolioTrade(tr, shares)
        engine.register_close(s, pt.pnl, now=now)
        trades.append(pt)

    for t, _pos, s, i in events:
        now = _utc(t)
        bars = bars_by_symbol[s]
        bar = bars[i]
        state = states[s]

        # Intrabar (1m) stop/target on THIS 5m window, when 1m bars exist for it.
        # The fill happens mid-window (before the close), so it takes precedence over
        # the close's crossover; record the exit at the 1m bar's time/price.
        window_1m = windows.get(s, {}).get(bar.get("time")) if s in windows else None
        if state.position is not None and window_1m:
            ex_action, ex_bar = resolve_intrabar_exit(state.position, window_1m)
            if ex_action is not None:
                _close(s, ex_action, i, ex_bar, _utc(ex_bar.get("time") or t))
                continue

        allow_long, allow_short = _gates(s, i)
        # Session (RTH) gating is applied inside decide_index — the single funnel —
        # so the portfolio sim, replay, and live can't diverge on it.
        action = decide_index(bars, series[s], i, state, params,
                              allow_long=allow_long, allow_short=allow_short,
                              can_open=True, must_flat=False,
                              external_fills=bool(window_1m))
        if action.kind == "enter":
            sizing = engine.check_and_size(s, action.price, action.stop, now=now)
            if sizing.quantity > 0:
                apply_action(action, state, i, bar, s)  # sets state.position
                engine.register_open(s, sizing.quantity, action.price, action.stop, now=now)
                open_shares[s] = sizing.quantity
            # rejected → leave flat, mirroring _handle_mp_action
        elif action.kind == "exit" and state.position is not None:
            _close(s, action, i, bar, now)

    # Close any still-open position at the end of its symbol's data (reason "end"),
    # so metrics are on realized trades — same as signals.replay.
    for s in symbols:
        state = states[s]
        if state.position is not None:
            bars = bars_by_symbol[s]
            last_i = len(bars) - 1
            last = bars[last_i]
            _close(s, Action("exit", state.position.side, last["close"], reason="end"),
                   last_i, last, _utc(last.get("time") or 0))

    return trades
