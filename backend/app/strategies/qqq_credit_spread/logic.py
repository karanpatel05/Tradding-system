"""Pure rules of the QQQ first-hour credit spread, ported from the external runner
(``~/live_system_maintenance/live/live_strategy.py`` and the backtester it imports).

Everything here is deterministic and network-free so it can be checked for parity
against the runner. Bars are the app's candle dicts:
``{"time": epoch_ms (bar START), "open", "high", "low", "close", "volume"}``.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
FEATURES = ["distance_short_otm", "distance_to_strike_pct", "distance_in_30m_vol",
            "rv_30", "momentum_5_bp", "momentum_30_bp", "volume_30m", "first_hour_range",
            "first_hour_body", "first_hour_volume", "is_put", "is_condor", "weekday",
            "early_close", "entry_credit_per_share"]
TICKER = "QQQ"
WIDTH = 1.0          # $1-wide vertical
INCREMENT = 1.0      # QQQ 0DTE strike grid


# --------------------------------------------------------------------------- calendar
@lru_cache(maxsize=1)
def _xnys():
    import exchange_calendars as ec
    return ec.get_calendar("XNYS")


@dataclass(frozen=True)
class SessionPlan:
    day: date
    open: datetime            # ET, aware
    close: datetime
    early_close: bool
    entry: datetime           # open + 1h  (10:30)
    checkpoint: datetime      # close - 30m (15:30 / 12:30)
    final_close: datetime     # close - final_close_seconds (15:50)
    close_deadline: datetime  # close - close_deadline_seconds (15:55)


def session_plan(d: date, final_close_seconds: int = 600,
                 close_deadline_seconds: int = 300) -> SessionPlan | None:
    """Action times for a session, or None when the exchange is shut (XNYS calendar,
    as the runner's ``iter_sessions``)."""
    cal = _xnys()
    key = d.isoformat()
    try:
        if not cal.is_session(key):
            return None
        o = cal.session_open(key).tz_convert(ET).to_pydatetime()
        c = cal.session_close(key).tz_convert(ET).to_pydatetime()
    except Exception:  # noqa: BLE001 - outside the calendar's range: fail closed
        return None
    early = c.hour < 16
    return SessionPlan(d, o, c, early, o + timedelta(hours=1), c - timedelta(minutes=30),
                       c - timedelta(seconds=final_close_seconds),
                       c - timedelta(seconds=close_deadline_seconds), )


def within_grace(target: datetime, now: datetime, grace_minutes: int) -> bool:
    """True only from the scheduled moment until ``grace_minutes`` after it."""
    delta = now - target
    return timedelta(0) <= delta <= timedelta(minutes=grace_minutes)


# --------------------------------------------------------------------------- symbols
def occ_symbol(day: date, right: str, strike: float, symbol: str = TICKER) -> str:
    """Unpadded OCC: root + YYMMDD + P/C + strike*1000 in 8 digits (Alpaca format)."""
    assert right in {"P", "C"} and strike > 0
    return f"{symbol}{day:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def ladder_quantity(equity: float, per_spread: float = 1000., first_spread: float = 500.) -> int:
    """Spreads affordable at this equity: one per ``per_spread``, the first at
    ``first_spread`` (verbatim from the backtester)."""
    if equity < first_spread:
        return 0
    return max(1, int(math.floor(equity / per_spread)))


# --------------------------------------------------------------------------- signal
@dataclass
class Signal:
    ticker: str
    session_date: str
    right: str                 # 'P' sells a put spread, 'C' a call spread
    short_strike: float
    long_strike: float
    short_symbol: str          # unpadded OCC
    long_symbol: str
    spot_at_entry: float
    first_hour_open: float
    first_hour_close: float
    first_hour_high: float
    first_hour_low: float
    first_hour_range: float
    first_hour_body: float
    first_hour_volume: float
    first_hour_vwap: float
    early_close: int
    weekday: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _window(bars: list[dict], start: datetime, end: datetime) -> list[dict]:
    lo, hi = _ms(start), _ms(end)
    rows = sorted((b for b in bars if lo <= int(b["time"]) < hi), key=lambda b: b["time"])
    out, seen = [], set()
    for b in rows:                      # one bar per minute (latest wins)
        m = int(b["time"]) // 60000
        if m in seen:
            out[-1] = b
        else:
            seen.add(m)
            out.append(b)
    return out


def first_hour_signal(bars: list[dict], plan: SessionPlan,
                      ticker: str = TICKER) -> tuple[Signal | None, str | None]:
    """The backtested first-hour rules on today's completed hour (runner parity):
    >=56 valid minutes with both endpoints, colour from close vs open, flat skipped,
    short strike rounded away from spot onto the grid."""
    open_ts, entry_ts = plan.open, plan.entry
    hour = _window(bars, open_ts, entry_ts)
    if len(hour) < 56:
        return None, "incomplete_first_hour"
    first, last = hour[0], hour[-1]
    if int(first["time"]) > _ms(open_ts) or int(last["time"]) < _ms(entry_ts - timedelta(minutes=1)):
        return None, "missing_first_or_last_hour_minute"
    try:
        values = np.array([[float(b[k]) for k in ("open", "high", "low", "close", "volume")]
                           for b in hour], dtype=float)
    except (TypeError, ValueError, KeyError):
        return None, "invalid_first_hour"
    if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any():
        return None, "invalid_first_hour"

    weights = values[:, 4]
    typical = (values[:, 1] + values[:, 2] + values[:, 3]) / 3
    traded = float(weights.sum())
    vwap = float((typical * weights).sum() / traded) if traded > 0 else float("nan")

    f_open, l_close = float(first["open"]), float(last["close"])
    if l_close == f_open:
        return None, "flat_first_hour"
    right = "P" if l_close > f_open else "C"

    step = INCREMENT
    short = float((math.floor(l_close / step + 1e-7) if right == "P"
                   else math.ceil(l_close / step - 1e-7)) * step)
    long = short - WIDTH if right == "P" else short + WIDTH
    if long <= 0:
        return None, "nonpositive_long_strike"
    spot = l_close
    if (spot - short if right == "P" else short - spot) <= 0:
        return None, "short_strike_itm_at_entry"

    expiry = plan.day
    low, high = float(values[:, 2].min()), float(values[:, 1].max())
    return Signal(
        ticker=ticker, session_date=expiry.isoformat(), right=right,
        short_strike=short, long_strike=long,
        short_symbol=occ_symbol(expiry, right, short, ticker),
        long_symbol=occ_symbol(expiry, right, long, ticker),
        spot_at_entry=spot, first_hour_open=f_open, first_hour_close=l_close,
        first_hour_high=high, first_hour_low=low, first_hour_range=high - low,
        first_hour_body=abs(l_close - f_open), first_hour_volume=float(weights.sum()),
        first_hour_vwap=vwap, early_close=int(plan.early_close),
        weekday=expiry.weekday()), None


# --------------------------------------------------------------------------- checkpoint
def checkpoint_features(bars: list[dict], signal: Signal, exit_ts: datetime,
                        credit_per_share: float) -> tuple[dict | None, str | None]:
    """The model's feature row from the 65 minutes before the checkpoint (runner parity)."""
    pre = _window(bars, exit_ts - timedelta(minutes=65), exit_ts)
    if len(pre) < 26:
        return None, "missing_checkpoint_bars"
    if int(pre[-1]["time"]) < _ms(exit_ts - timedelta(minutes=1)):
        return None, "late_checkpoint_data"
    prev = np.array([float(b["close"]) for b in pre], dtype=float)
    logrets = np.diff(np.log(prev)) if len(prev) > 1 else np.array([])
    rv = float(np.std(logrets[-30:]) * np.sqrt(30)) if len(logrets) > 10 else 0.0
    put_side = signal.right == "P"
    close = float(prev[-1])
    dist = (close - signal.short_strike) if put_side else (signal.short_strike - close)
    vols = [float(b.get("volume") or 0.0) for b in pre]
    row = {
        "distance_short_otm": dist,
        "distance_to_strike_pct": dist / close * 100,
        "distance_in_30m_vol": dist / (close * max(rv, 1e-6)),
        "rv_30": rv,
        "momentum_5_bp": float((prev[-1] / prev[-6] - 1) * 1e4) if len(prev) >= 6 else 0,
        "momentum_30_bp": float((prev[-1] / prev[-31] - 1) * 1e4) if len(prev) >= 31 else 0,
        "volume_30m": float(sum(vols[-30:])),
        "first_hour_range": signal.first_hour_range,
        "first_hour_body": signal.first_hour_body,
        "first_hour_volume": signal.first_hour_volume,
        "is_put": int(put_side),
        "is_condor": 0,
        "weekday": signal.weekday,
        "early_close": signal.early_close,
        "entry_credit_per_share": credit_per_share,
    }
    return row, None


# --------------------------------------------------------------------------- prices
def spread_limit_credit(quotes: dict, short_occ: str, long_occ: str,
                        offset: float = 0.0) -> tuple[float | None, str | None]:
    """Mid credit for the vertical: short mid - long mid - offset (runner parity)."""
    short, long = quotes.get(short_occ), quotes.get(long_occ)
    if not short or not long or short.get("mid") is None or long.get("mid") is None:
        return None, "missing_option_quote"
    credit = round(short["mid"] - long["mid"] - offset, 4)
    if credit <= 0:
        return None, "nonpositive_credit_at_mid"
    return credit, None


def closing_limit_debit(quotes: dict, short_occ: str, long_occ: str, width: float,
                        progress: float) -> float:
    """Walk from the mid toward ask(short) - bid(long), capped at the spread width
    (runner parity). Missing/invalid quotes fall back to the full width."""
    short, long = quotes.get(short_occ), quotes.get(long_occ)
    if not short or not long:
        return round(width, 2)
    values = [short.get("bid"), short.get("ask"), long.get("bid"), long.get("ask")]
    if any(v is None or not math.isfinite(float(v)) or float(v) < 0 for v in values):
        return round(width, 2)
    sb, sa, lb, la = map(float, values)
    if sa < sb or la < lb or sa <= 0:
        return round(width, 2)
    mid = max(0., (sb + sa - lb - la) / 2)
    marketable = max(.01, sa - lb)
    price = mid + min(1., max(0., progress)) * max(0., marketable - mid)
    return min(round(width, 2), max(.01, math.ceil((price - 1e-10) * 100) / 100))


def close_progress(now: datetime, plan: SessionPlan, checkpoint: bool,
                   final_close_seconds: int = 600, close_deadline_seconds: int = 300) -> float:
    """0 at the stage start, 1 when the limit should be fully marketable (runner parity)."""
    start = (plan.close - timedelta(minutes=30) if checkpoint
             else plan.close - timedelta(seconds=final_close_seconds))
    duration = 120. if checkpoint else max(1., final_close_seconds - close_deadline_seconds - 60.)
    return max(0., min(1., (now - start).total_seconds() / duration))


def checkpoint_stage_deadline(now: datetime, plan: SessionPlan) -> datetime:
    """How long a checkpoint close may work before the final sweep takes over."""
    return min(plan.final_close, max(plan.checkpoint + timedelta(minutes=3),
                                     now + timedelta(minutes=1)))
