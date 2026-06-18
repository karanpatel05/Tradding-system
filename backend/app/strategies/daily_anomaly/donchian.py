"""Donchian-breakout trend-following on RTH daily bars (long-only).

Entry: close breaks the prior N-day high -> buy next open (market). Stop: entry minus
k * ATR(14). Exit: stop hit, or close back below the prior N/2-day low, whichever comes
first; else mark out at end of data. Risk-fractional sizing (1% of capital per trade).
Long-only by design — shorting breakouts of high-beta names is a known trap.
"""

from __future__ import annotations

import math


def atr(bars: list[dict], period: int = 14) -> list[float]:
    """Average True Range, aligned to ``bars`` (partial trailing window before
    ``period`` bars exist, so it is usable on short series)."""
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            tr.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    out = []
    for i in range(len(bars)):
        window = tr[max(0, i - period + 1):i + 1]
        out.append(sum(window) / len(window))
    return out


def simulate_donchian(bars: list[dict], *, n: int = 20, k: float = 2.0, capital: float = 2000.0,
                      risk_pct: float = 0.01, slip_bps: float = 2.0) -> list[dict]:
    """Run the breakout system over one symbol's daily bars; return realized trades."""
    if len(bars) < n + 2:
        return []
    a = atr(bars, period=14)
    slip = slip_bps / 10_000.0
    exit_lookback = max(1, n // 2)
    trades: list[dict] = []
    pos = None
    pending = None  # (signal_index) -> enter at next open

    for i, b in enumerate(bars):
        # 1) Resolve an open position on this bar (stop intrabar, then donchian close exit).
        if pos is not None:
            if b["low"] <= pos["stop"]:
                trades.append(_close(pos, pos["stop"], "stop", b["date"], slip))
                pos = None
            else:
                prior_low = min(x["low"] for x in bars[i - exit_lookback:i]) if i >= exit_lookback else None
                if prior_low is not None and b["close"] < prior_low:
                    trades.append(_close(pos, b["close"], "donchian_exit", b["date"], slip))
                    pos = None

        # 2) Fill a pending entry at this bar's open.
        if pos is None and pending is not None:
            sig = pending
            entry = b["open"] * (1 + slip)
            stop = entry - k * a[sig]
            risk = entry - stop
            if risk > 0 and entry > 0:
                shares = min(int((capital * risk_pct) / risk), int(capital / entry))
                if shares >= 1:
                    pos = {"side": "long", "entry_price": round(b["open"], 4), "stop": stop,
                           "shares": shares, "entry_date": b["date"]}
            pending = None

        # 3) Look for a new breakout (only when flat and not already pending).
        if pos is None and pending is None and i >= n:
            prior_high = max(x["high"] for x in bars[i - n:i])
            if b["close"] > prior_high:
                pending = i

    if pos is not None:
        last = bars[-1]
        trades.append(_close(pos, last["close"], "eod", last["date"], slip))
    return trades


def _close(pos: dict, exit_price: float, reason: str, date: str, slip: float) -> dict:
    entry, shares = pos["entry_price"], pos["shares"]
    fill = exit_price * (1 - slip) if reason != "stop" else exit_price  # stop assumed at level
    gross = (fill - entry) * shares
    ret = (fill - entry) / entry if entry else 0.0
    return {"side": "long", "entry_date": pos["entry_date"], "date": date,
            "entry_price": entry, "exit_price": round(fill, 4), "exit_reason": reason,
            "shares": shares, "net": round(gross, 2), "ret": ret}
