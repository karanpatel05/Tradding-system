"""The single signal core — shared verbatim by backtest and live.

``generate_signal`` is a pure function of (bars-so-far, current state, params,
regime/session flags). It returns an :class:`Action`; the caller applies it with
:func:`apply_action`. The backtest is :func:`replay` feeding the function growing
slices; the live worker feeds it each closed 5-min bar. One decision function +
one set of price levels ⇒ backtest↔live parity by construction.

Logic (9/15 EMA scalping spec):
* **Trend:** EMA9 vs EMA15, price the right side of EMA15, and the **EMA15
  linear-regression slope over N candles** beyond ``slope_min × ATR`` (objective —
  no visual angles).
* **Sideways reject:** stand down when ``|EMA9 − EMA15| < sideways × ATR``.
* **Entry validation:** reject extended entries where ``|price − EMA15| > max × ATR``.
* **Three typed entries** (recorded on the trade): (1) pullback + pin bar, (2)
  pullback + engulfing, (3) close beyond the zone then a strong reversal back
  across both EMAs. Trigger = break of that candle's high (long) / low (short).
* **Stop** beyond the entry candle ± ``stop_buffer × ATR``; **target** at 1:2.

Trigger/fill (also the no-lookahead rule): the confirmation candle is judged on
its **close**; the entry fills on a *later* bar that trades through the trigger,
using only that bar's high/low.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.strategies.ema_scalper import indicators as ind
from app.strategies.ema_scalper.params import EmaScalperParams


@dataclass
class Position:
    side: str  # 'long' | 'short'
    entry: float
    stop: float
    target: float
    entry_index: int
    entry_type: str = ""
    entry_time: int | None = None


@dataclass
class Pending:
    side: str
    trigger: float
    stop: float
    target: float
    armed_index: int
    entry_type: str = ""


@dataclass
class State:
    position: Position | None = None
    pending: Pending | None = None


@dataclass
class Action:
    kind: str  # 'arm' | 'cancel' | 'enter' | 'exit' | 'none'
    side: str | None = None
    price: float | None = None
    stop: float | None = None
    target: float | None = None
    entry_type: str = ""
    reason: str = ""


@dataclass
class Trade:
    side: str
    entry_price: float
    exit_price: float
    entry_index: int
    exit_index: int
    entry_time: int | None
    exit_time: int | None
    reason: str
    entry_type: str = ""
    stop: float = 0.0
    target: float = 0.0

    @property
    def return_pct(self) -> float:
        if not self.entry_price:
            return 0.0
        raw = (self.exit_price - self.entry_price) / self.entry_price
        return raw if self.side == "long" else -raw

    @property
    def r_multiple(self) -> float:
        """Realized profit in units of initial risk (entry − stop)."""
        risk = abs(self.entry_price - self.stop)
        if risk <= 0:
            return 0.0
        move = (self.exit_price - self.entry_price) if self.side == "long" else (self.entry_price - self.exit_price)
        return move / risk


_NONE = Action("none")


def generate_signal(
    bars: list[dict],
    state: State,
    params: EmaScalperParams,
    *,
    allow_long: bool = True,
    allow_short: bool = True,
    can_open: bool = True,
    must_flat: bool = False,
) -> Action:
    n = len(bars)
    if n < params.warmup_bars:
        return _NONE

    i = n - 1
    cur = bars[i]
    prev = bars[i - 1] if i >= 1 else None

    closes = [b["close"] for b in bars]
    ef = ind.ema(closes, params.ema_fast)
    es = ind.ema(closes, params.ema_slow)
    at = ind.atr(bars, params.atr_length)
    fast, slow, atr = ef[i], es[i], at[i]
    if fast is None or slow is None or not atr:
        return _NONE

    # 1) Exits first.
    if state.position is not None:
        return _exit_action(state.position, cur, prev, must_flat)

    # 2) Armed-but-unfilled trigger.
    if state.pending is not None:
        return _pending_action(state.pending, cur, fast, slow, i, params)

    # 3) Flat: hunt for a fresh, confirmed setup inside the entry window.
    if not can_open or must_flat:
        return _NONE

    # Sideways/chop reject: EMAs too close together.
    if abs(fast - slow) < params.sideways_min_sep_atr * atr:
        return _NONE

    # Entry validation: reject extended price (chasing momentum).
    if abs(cur["close"] - slow) > params.max_entry_distance_atr * atr:
        return _NONE

    # EMA15 linear-regression slope over the lookback window.
    window = [v for v in es[i - params.slope_lr_lookback + 1: i + 1] if v is not None]
    if len(window) < params.slope_lr_lookback:
        return _NONE
    slope15 = ind.linreg_slope(window)
    thresh = params.slope_min * atr

    zone_hi = max(fast, slow)
    zone_lo = min(fast, slow)
    touched = cur["low"] <= zone_hi and cur["high"] >= zone_lo

    bull = allow_long and fast > slow and cur["close"] > slow and slope15 >= thresh
    bear = allow_short and fast < slow and cur["close"] < slow and slope15 <= -thresh

    if bull:
        etype = _long_entry_type(prev, cur, fast, slow, zone_hi, zone_lo, touched)
        if etype:
            return _arm("long", etype, trigger=cur["high"], candle_extreme=cur["low"],
                        atr=atr, params=params)

    if bear:
        etype = _short_entry_type(prev, cur, fast, slow, zone_hi, zone_lo, touched)
        if etype:
            return _arm("short", etype, trigger=cur["low"], candle_extreme=cur["high"],
                        atr=atr, params=params)

    return _NONE


def _long_entry_type(prev, cur, fast, slow, zone_hi, zone_lo, touched) -> str:
    """Pick the long entry type (or '') for a confirmed bullish candle that closed
    back above EMA15."""
    if touched and ind.bullish_hammer(cur):
        return "pin"
    if touched and prev is not None and ind.bullish_engulfing(prev, cur):
        return "engulfing"
    # Type 3: prior close below the whole zone, strong reversal back above both EMAs.
    if prev is not None and prev["close"] < zone_lo and ind.strong_bull_body(cur) \
            and cur["close"] > zone_hi:
        return "reversal"
    return ""


def _short_entry_type(prev, cur, fast, slow, zone_hi, zone_lo, touched) -> str:
    if touched and ind.bearish_star(cur):
        return "pin"
    if touched and prev is not None and ind.bearish_engulfing(prev, cur):
        return "engulfing"
    if prev is not None and prev["close"] > zone_hi and ind.strong_bear_body(cur) \
            and cur["close"] < zone_lo:
        return "reversal"
    return ""


def _arm(side: str, entry_type: str, *, trigger: float, candle_extreme: float,
         atr: float, params: EmaScalperParams) -> Action:
    """Arm a stop entry at ``trigger`` with a stop just beyond the entry candle
    (± buffer) and a 1:RR target."""
    buf = params.stop_buffer_atr * atr
    if side == "long":
        stop = candle_extreme - buf
        risk = trigger - stop
        target = trigger + params.risk_reward * risk
    else:
        stop = candle_extreme + buf
        risk = stop - trigger
        target = trigger - params.risk_reward * risk
    if risk <= 0:
        return _NONE
    return Action("arm", side=side, price=trigger, stop=stop, target=target, entry_type=entry_type)


def _pending_action(p: Pending, cur: dict, fast: float, slow: float,
                    i: int, params: EmaScalperParams) -> Action:
    trend_ok = (p.side == "long" and fast > slow) or (p.side == "short" and fast < slow)
    if not trend_ok or (i - p.armed_index) > params.entry_expiry_bars:
        return Action("cancel")
    if p.side == "long" and cur["high"] >= p.trigger:
        fill = max(p.trigger, cur["open"]) if cur["open"] > p.trigger else p.trigger
        return Action("enter", side="long", price=fill, stop=p.stop, target=p.target,
                      entry_type=p.entry_type)
    if p.side == "short" and cur["low"] <= p.trigger:
        fill = min(p.trigger, cur["open"]) if cur["open"] < p.trigger else p.trigger
        return Action("enter", side="short", price=fill, stop=p.stop, target=p.target,
                      entry_type=p.entry_type)
    return _NONE


def _exit_action(pos: Position, cur: dict, prev: dict | None, must_flat: bool) -> Action:
    if must_flat:
        return Action("exit", price=cur["close"], reason="session_cutoff")
    if pos.side == "long":
        if cur["low"] <= pos.stop:
            return Action("exit", price=pos.stop, reason="stop")
        if cur["high"] >= pos.target:
            return Action("exit", price=pos.target, reason="target")
        if ind.strong_bear_body(cur) or (prev is not None and ind.bearish_engulfing(prev, cur)):
            return Action("exit", price=cur["close"], reason="reversal")
    else:
        if cur["high"] >= pos.stop:
            return Action("exit", price=pos.stop, reason="stop")
        if cur["low"] <= pos.target:
            return Action("exit", price=pos.target, reason="target")
        if ind.strong_bull_body(cur) or (prev is not None and ind.bullish_engulfing(prev, cur)):
            return Action("exit", price=cur["close"], reason="reversal")
    return _NONE


def apply_action(action: Action, state: State, bar_index: int, bar: dict,
                 *, slippage_pct: float = 0.0) -> Trade | None:
    if action.kind == "arm":
        state.pending = Pending(action.side, action.price, action.stop, action.target,
                                bar_index, action.entry_type)
        return None
    if action.kind == "cancel":
        state.pending = None
        return None
    if action.kind == "enter":
        fill = _slip(action.price, action.side, slippage_pct, opening=True)
        state.position = Position(action.side, fill, action.stop, action.target,
                                  bar_index, action.entry_type, bar.get("time"))
        state.pending = None
        return None
    if action.kind == "exit" and state.position is not None:
        pos = state.position
        fill = _slip(action.price, pos.side, slippage_pct, opening=False)
        trade = Trade(pos.side, pos.entry, fill, pos.entry_index, bar_index,
                      pos.entry_time, bar.get("time"), action.reason, pos.entry_type,
                      pos.stop, pos.target)
        state.position = None
        return trade
    return None


def _slip(price: float, side: str, slippage_pct: float, *, opening: bool) -> float:
    if not slippage_pct:
        return price
    up = (side == "long") == opening
    return price * (1 + slippage_pct) if up else price * (1 - slippage_pct)


def replay(
    bars: list[dict],
    params: EmaScalperParams,
    *,
    allow_long: list[bool] | None = None,
    allow_short: list[bool] | None = None,
    can_open: list[bool] | None = None,
    must_flat: list[bool] | None = None,
    slippage_pct: float = 0.0,
) -> list[Trade]:
    """Event-driven backtest: feed the SAME ``generate_signal`` growing slices."""
    state = State()
    trades: list[Trade] = []
    n = len(bars)

    def flag(arr: list[bool] | None, idx: int, default: bool) -> bool:
        return default if arr is None else arr[idx]

    for i in range(params.warmup_bars, n):
        window = bars[: i + 1]
        action = generate_signal(
            window, state, params,
            allow_long=flag(allow_long, i, True),
            allow_short=flag(allow_short, i, True),
            can_open=flag(can_open, i, True),
            must_flat=flag(must_flat, i, False),
        )
        trade = apply_action(action, state, i, bars[i], slippage_pct=slippage_pct)
        if trade is not None:
            trades.append(trade)

    if state.position is not None and n:
        last = bars[n - 1]
        trade = apply_action(Action("exit", price=last["close"], reason="eod"),
                             state, n - 1, last, slippage_pct=slippage_pct)
        if trade is not None:
            trades.append(trade)
    return trades
