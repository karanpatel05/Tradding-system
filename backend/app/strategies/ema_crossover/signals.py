"""Pure 9/15 EMA crossover signal logic (stock-only, long + short).

ONE decision function (`_decide`) drives both live and backtest so they can't
diverge: ``generate_signal`` computes the indicator scalars from a growing bar
slice (live, one call per closed bar); ``replay`` precomputes the series once and
feeds the same scalars per bar (backtest). EMA/ATR are causal, so slice-vs-full
give identical values at each index — parity by construction (asserted in tests).

Entry (bullish): EMA9 crosses ABOVE EMA15, close above both EMAs, volume >
multiplier × avg, and both EMA slopes positive. Bearish mirrors. Exit: opposite
crossover, stop, or target. An optional 15m HTF trend gate is applied by the
caller via the ``allow_long`` / ``allow_short`` flags.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.strategies.ema_crossover.params import EmaCrossoverParams
from app.strategies.ema_crossover.session import session_flags
from app.strategies.ema_scalper import indicators as ind

Bar = dict


@dataclass
class Position:
    side: str          # 'long' | 'short'
    entry_price: float
    stop: float
    target: float
    entry_index: int
    entry_time: int | None


@dataclass
class State:
    position: Position | None = None


@dataclass
class Action:
    kind: str                      # 'enter' | 'exit' | 'none'
    side: str | None = None        # 'long' | 'short'
    price: float | None = None
    stop: float | None = None
    target: float | None = None
    reason: str = ""               # 'crossover' | 'stop' | 'target' | 'eod' | 'end'


@dataclass
class Trade:
    symbol: str
    side: str
    entry_index: int
    exit_index: int
    entry_time: int | None
    exit_time: int | None
    entry_price: float
    exit_price: float
    stop: float
    target: float
    reason: str

    @property
    def return_pct(self) -> float:
        if not self.entry_price:
            return 0.0
        raw = self.exit_price / self.entry_price - 1.0
        return raw if self.side == "long" else -raw

    @property
    def r_multiple(self) -> float:
        risk = abs(self.entry_price - self.stop)
        if not risk:
            return 0.0
        gain = (self.exit_price - self.entry_price) * (1 if self.side == "long" else -1)
        return gain / risk


def direction_gates(direction: str, htf_long: bool = True, htf_short: bool = True) -> tuple[bool, bool]:
    """Combine the trade-direction mode with the HTF trend gate into
    ``(allow_long, allow_short)``. THE single place direction is applied, so the
    live worker and the portfolio backtest gate entries identically (parity)."""
    allow_long = direction in ("long", "both") and htf_long
    allow_short = direction in ("short", "both") and htf_short
    return allow_long, allow_short


def _cross_up(pf, ps, f, s) -> bool:
    return None not in (pf, ps, f, s) and pf <= ps and f > s


def _cross_down(pf, ps, f, s) -> bool:
    return None not in (pf, ps, f, s) and pf >= ps and f < s


def resolve_intrabar_exit(position: Position, bars_1m: list[Bar]) -> tuple[Action | None, Bar | None]:
    """Resolve a position's stop/target by walking finer (1-minute) bars in order —
    the higher-fidelity fill model. Returns ``(exit_action, exit_bar)`` for the
    FIRST 1m bar that touches a level, else ``(None, None)``.

    THE shared intrabar resolver: the backtest and paper-live both call this so they
    fill identically, and real-live's broker OTOCO is its real-world counterpart.
    Within a single 1m bar that touches BOTH levels the **stop wins** (the same safe,
    pessimistic tie-break the 5m path uses) — 1m shrinks that ambiguity ~5×, it
    doesn't remove it."""
    for b in bars_1m:
        if position.side == "long":
            if b["low"] <= position.stop:
                return Action("exit", "long", position.stop, reason="stop"), b
            if b["high"] >= position.target:
                return Action("exit", "long", position.target, reason="target"), b
        else:  # short
            if b["high"] >= position.stop:
                return Action("exit", "short", position.stop, reason="stop"), b
            if b["low"] <= position.target:
                return Action("exit", "short", position.target, reason="target"), b
    return None, None


def _decide(*, prev_fast, prev_slow, fast, slow, slope_fast, slope_slow, atr,
            vol, avg_vol, bar: Bar, state: State, params: EmaCrossoverParams,
            allow_long: bool, allow_short: bool, can_open: bool, must_flat: bool,
            external_fills: bool = False) -> Action:
    """Scalar-in, Action-out. The single source of truth for entries/exits.

    ``external_fills=True`` means stop/target are resolved elsewhere (intrabar 1m
    sim or a live broker OTOCO), so the open-position branch checks only EOD and the
    opposite-crossover exit — it must NOT also fire stop/target off the 5m bar, or it
    would double-count what the finer resolver/broker already owns."""
    close = bar["close"]

    # --- manage an open position first ---
    if state.position is not None:
        pos = state.position
        if pos.side == "long":
            if must_flat:
                return Action("exit", "long", close, reason="eod")
            if not external_fills:
                if bar["low"] <= pos.stop:
                    return Action("exit", "long", pos.stop, reason="stop")
                if bar["high"] >= pos.target:
                    return Action("exit", "long", pos.target, reason="target")
            if _cross_down(prev_fast, prev_slow, fast, slow):
                return Action("exit", "long", close, reason="crossover")
        else:  # short
            if must_flat:
                return Action("exit", "short", close, reason="eod")
            if not external_fills:
                if bar["high"] >= pos.stop:
                    return Action("exit", "short", pos.stop, reason="stop")
                if bar["low"] <= pos.target:
                    return Action("exit", "short", pos.target, reason="target")
            if _cross_up(prev_fast, prev_slow, fast, slow):
                return Action("exit", "short", close, reason="crossover")
        return Action("none")

    # --- look for an entry while flat ---
    if not can_open or None in (fast, slow, prev_fast, prev_slow, atr, avg_vol, slope_fast, slope_slow):
        return Action("none")
    vol_ok = vol > params.vol_multiplier * avg_vol

    if (allow_long and _cross_up(prev_fast, prev_slow, fast, slow)
            and close > fast and close > slow and vol_ok and slope_fast > 0 and slope_slow > 0):
        stop = close - params.stop_atr_mult * atr
        target = close + params.risk_reward * (close - stop)
        return Action("enter", "long", close, stop=stop, target=target)

    if (allow_short and _cross_down(prev_fast, prev_slow, fast, slow)
            and close < fast and close < slow and vol_ok and slope_fast < 0 and slope_slow < 0):
        stop = close + params.stop_atr_mult * atr
        target = close - params.risk_reward * (stop - close)
        return Action("enter", "short", close, stop=stop, target=target)

    return Action("none")


def apply_action(action: Action, state: State, i: int, bar: Bar, symbol: str = "") -> Trade | None:
    """Mutate state for an enter/exit; return the realized Trade on exit."""
    if action.kind == "enter":
        state.position = Position(action.side, action.price, action.stop, action.target,
                                  entry_index=i, entry_time=bar.get("time"))
        return None
    if action.kind == "exit" and state.position is not None:
        pos = state.position
        trade = Trade(
            symbol=symbol, side=pos.side, entry_index=pos.entry_index, exit_index=i,
            entry_time=pos.entry_time, exit_time=bar.get("time"),
            entry_price=pos.entry_price, exit_price=action.price,
            stop=pos.stop, target=pos.target, reason=action.reason,
        )
        state.position = None
        return trade
    return None


class Series:
    """Precomputed indicator series for a symbol's bars (EMA fast/slow, ATR, volume).

    Computed once per symbol; ``decide_index`` reads scalars out of it per bar. The
    single place the per-index indicator math lives, so live (``generate_signal``),
    backtest (``replay``) and the portfolio sim all decide off identical inputs."""

    __slots__ = ("fast", "slow", "atr", "vols")

    def __init__(self, bars: list[Bar], params: EmaCrossoverParams) -> None:
        closes = [b["close"] for b in bars]
        self.fast = ind.ema(closes, params.fast)
        self.slow = ind.ema(closes, params.slow)
        self.atr = ind.atr(bars, params.atr_length)
        self.vols = [b.get("volume") or 0.0 for b in bars]


def _avg_vol(vols: list[float], i: int, window: int) -> float | None:
    if i < window:
        return None
    w = vols[i - window:i]  # prior window, excludes the current bar
    return sum(w) / len(w) if w else None


def decide_index(bars: list[Bar], series: Series, i: int, state: State,
                 params: EmaCrossoverParams, *, allow_long: bool, allow_short: bool,
                 can_open: bool, must_flat: bool, external_fills: bool = False) -> Action:
    """Decision for bar ``i`` given a precomputed ``series`` and the current state.

    THE single decision entry point — live, backtest replay, and the portfolio sim
    all funnel through here, so they can't diverge. ``i < 1`` yields no action
    (a crossover needs a previous bar). ``external_fills`` is forwarded to ``_decide``
    so callers that resolve stop/target intrabar (1m sim / broker OTOCO) suppress the
    5m stop/target check here."""
    if i < 1:
        return Action("none")
    fast, slow, atr, vols = series.fast, series.slow, series.atr, series.vols
    j = i - params.slope_lookback
    # RTH session gate, applied HERE (the single funnel) so every caller — live,
    # backtest replay, portfolio sim — gates entries/EOD identically. AND/OR with the
    # caller's own gates so an explicit ``can_open=False``/``must_flat=True`` still wins.
    sess_open, sess_flat = session_flags(bars[i].get("time"), params)
    return _decide(
        prev_fast=fast[i - 1], prev_slow=slow[i - 1], fast=fast[i], slow=slow[i],
        slope_fast=(fast[i] - fast[j]) if j >= 0 and fast[i] is not None and fast[j] is not None else None,
        slope_slow=(slow[i] - slow[j]) if j >= 0 and slow[i] is not None and slow[j] is not None else None,
        atr=atr[i], vol=vols[i], avg_vol=_avg_vol(vols, i, params.vol_avg_window),
        bar=bars[i], state=state, params=params,
        allow_long=allow_long, allow_short=allow_short,
        can_open=can_open and sess_open, must_flat=must_flat or sess_flat,
        external_fills=external_fills,
    )


def generate_signal(bars: list[Bar], state: State, params: EmaCrossoverParams, *,
                    allow_long: bool = True, allow_short: bool = True,
                    can_open: bool = True, must_flat: bool = False,
                    external_fills: bool = False) -> Action:
    """Decision for the latest (closed) bar — used by the live worker. With
    ``external_fills=True`` the worker's bracket/1m resolver owns stop/target, so
    this returns only entries, the opposite-crossover exit, and EOD."""
    i = len(bars) - 1
    if i < 1:
        return Action("none")
    return decide_index(bars, Series(bars, params), i, state, params,
                        allow_long=allow_long, allow_short=allow_short,
                        can_open=can_open, must_flat=must_flat, external_fills=external_fills)


def replay(bars: list[Bar], params: EmaCrossoverParams, *, symbol: str = "",
           allow_long: list[bool] | None = None, allow_short: list[bool] | None = None,
           can_open: list[bool] | None = None, must_flat: list[bool] | None = None) -> list[Trade]:
    """Replay the same `_decide` across all bars → realized trades (backtest)."""
    n = len(bars)
    if n == 0:
        return []
    series = Series(bars, params)
    AL = allow_long or [True] * n
    AS = allow_short or [True] * n
    CO = can_open or [True] * n
    MF = must_flat or [False] * n

    state = State()
    trades: list[Trade] = []
    for i in range(1, n):
        action = decide_index(bars, series, i, state, params,
                              allow_long=AL[i], allow_short=AS[i],
                              can_open=CO[i], must_flat=MF[i])
        if action.kind != "none":
            t = apply_action(action, state, i, bars[i], symbol)
            if t:
                trades.append(t)

    # Close any position still open at the end of data so metrics are on realized trades.
    if state.position is not None:
        last = bars[-1]
        t = apply_action(Action("exit", state.position.side, last["close"], reason="end"),
                         state, n - 1, last, symbol)
        if t:
            trades.append(t)
    return trades
