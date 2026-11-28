"""Reprice the registered QQQ first-hour spread with historical one-minute bars."""
from datetime import date, datetime, time, timedelta
from math import isfinite

import httpx

from app.services.qqq_backtest_report import DAILY_FIELDS as FIELDS
from app.strategies.qqq_credit_spread.logic import ET, WIDTH, SessionPlan, first_hour_signal
from app.strategies.qqq_credit_spread.params import QqqSpreadParams
from scripts.daily_backtest import historical_bars


PARAMS = QqqSpreadParams()
MINUTE = timedelta(minutes=1)
# A one-cent bid/ask width on each leg costs half a cent per fill on two legs.
SPREAD_COST = 0.01
PAIR_WINDOW = timedelta(minutes=2)
INTRINSIC_WINDOW = timedelta(minutes=15)


def _timestamp(bar):
    ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("Historical QQQ bar has no timezone")
    return ts


def _plan(session):
    day = date.fromisoformat(session["date"])
    opened = datetime.combine(day, time.fromisoformat(session["open"]), ET)
    closed = datetime.combine(day, time.fromisoformat(session["close"]), ET)
    return SessionPlan(day, opened, closed, closed.hour < 16, opened + timedelta(hours=1),
                       closed - timedelta(seconds=PARAMS.final_close_seconds),
                       closed - timedelta(seconds=PARAMS.close_deadline_seconds))


def _signal(stocks, plan):
    hour = []
    for bar in stocks:
        start = _timestamp(bar)
        if plan.open <= start < plan.entry:
            hour.append({"time": int(start.timestamp() * 1000), "open": bar.get("o"),
                         "high": bar.get("h"), "low": bar.get("l"), "close": bar.get("c"),
                         "volume": bar.get("v")})
    return first_hour_signal(hour, plan)


def _closes(bars, earliest, latest):
    """Eligible marks keyed by completion time (Alpaca timestamps mark bar start)."""
    values = {}
    for bar in bars:
        completed = _timestamp(bar) + MINUTE
        if earliest <= completed <= latest:
            value = bar.get("c")
            if isinstance(value, (float, int)) and isfinite(value) and value > 0:
                values[completed] = value
    return values


def _paired_value(short_bars, long_bars, earliest, latest, pick):
    short = _closes(short_bars, earliest, latest)
    long = _closes(long_bars, earliest, latest)
    chosen = pick(short.keys() & long.keys(), default=None)
    if chosen is None:
        return None
    return round(short[chosen] - long[chosen], 4)


def _price(plan, stocks, load_options):
    row = dict.fromkeys(FIELDS, "")
    row["date"] = plan.day.isoformat()
    signal, reason = _signal(stocks, plan)
    if signal is None:
        rule_skips = {"flat_first_hour", "short_strike_itm_at_entry", "nonpositive_long_strike"}
        return dict(row, status="skipped" if reason in rule_skips else "missing", reason=reason)

    row.update(side="put" if signal.right == "P" else "call",
               short_strike=f"{signal.short_strike:g}", long_strike=f"{signal.long_strike:g}")
    options = load_options(signal)
    short = options.get(signal.short_symbol, [])
    long = options.get(signal.long_symbol, [])

    # The signal is fixed at 10:30. Use the first simultaneous pair of fresh marks.
    mid = _paired_value(short, long, plan.entry + MINUTE,
                        plan.entry + timedelta(minutes=PARAMS.grace_minutes), min)
    if mid is None:
        return dict(row, status="missing", reason="missing_paired_entry")
    if not 0 <= mid < WIDTH:
        return dict(row, status="missing", reason="incoherent_entry_mark")
    credit = round(mid - SPREAD_COST, 2)
    if credit < PARAMS.min_credit:
        return dict(row, status="skipped", reason="below_min_credit")
    row["entry_credit"] = f"{credit:.2f}"

    mid = _paired_value(short, long, plan.final_close - PAIR_WINDOW, plan.final_close, max)
    if mid is not None and 0 <= mid <= WIDTH:
        marked_debit = mid
        method = "bar"
    else:
        closes = _closes(stocks, plan.final_close - INTRINSIC_WINDOW, plan.final_close)
        if not closes:
            return dict(row, status="unresolved", reason="missing_exit_valuation")
        spot = closes[max(closes)]
        intrinsic = signal.short_strike - spot if signal.right == "P" else spot - signal.short_strike
        marked_debit = min(WIDTH, max(0.0, intrinsic))
        method = "intrinsic"

    debit = round(marked_debit + SPREAD_COST, 2)
    return dict(row, status="traded", exit_debit=f"{debit:.2f}", exit_method=method,
                net_pnl_usd=f"{(credit - debit) * 100:.2f}")


def price_bars(session: dict, stocks: list, options: dict) -> dict:
    """Price one exchange session using raw Alpaca stock and OCC-keyed option bars."""
    return _price(_plan(session), stocks, lambda signal: options)


def price_session(client: httpx.Client, session: dict) -> dict:
    """Fetch one SIP stock session and the selected option pair. HTTP failures propagate."""
    plan = _plan(session)
    span = {"start": plan.open.isoformat(), "end": plan.close.isoformat(),
            "timeframe": "1Min", "limit": 10_000}
    stocks = historical_bars(client, "/v2/stocks/bars", symbols="QQQ", feed="sip", adjustment="raw", **span)

    def load_options(signal):
        return historical_bars(client, "/v1beta1/options/bars",
                     symbols=f"{signal.short_symbol},{signal.long_symbol}", **span)

    return _price(plan, stocks.get("QQQ", []), load_options)
