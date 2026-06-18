"""Volume-confirmed Donchian breakout on 1-hour candles with a 2×ATR trailing stop.

Spec under test: track the 20-period high/low. Break above the 20-high with volume >=
1.5× the 20-period average volume -> go long; break below the 20-low with the same volume
confirmation -> go short (and exit any long). Trail the exit at 2×ATR.

1-hour bars are aggregated from cached intraday bars (5m / 15m) into 60-minute session
bins from 09:30 ET. Long + short, stop-and-reverse on an opposite confirmed breakout, plus
a chandelier-style trailing stop (extreme since entry ∓ atr_mult × ATR-at-entry). Entry/exit
fill on the bar close / stop level; slippage on every (market) leg.
"""

from __future__ import annotations

from datetime import time

from app.strategies.daily_anomaly.donchian import atr
from app.strategies.daily_anomaly.rth_daily import et_date, et_time

_OPEN_MIN = 9 * 60 + 30
_CLOSE = time(16, 0)


def aggregate_hourly(intraday_bars: list[dict]) -> list[dict]:
    """Aggregate RTH intraday bars into 60-min session bars (09:30→10:30, …, 15:30→16:00)."""
    buckets: dict[tuple, dict] = {}
    order: list[tuple] = []
    for b in sorted(intraday_bars, key=lambda x: x["time"]):
        t = et_time(b["time"])
        mins = t.hour * 60 + t.minute - _OPEN_MIN
        if mins < 0 or t >= _CLOSE:
            continue
        key = (et_date(b["time"]), mins // 60)
        if key not in buckets:
            buckets[key] = {"date": key[0], "open": b["open"], "high": b["high"],
                            "low": b["low"], "close": b["close"],
                            "volume": b.get("volume", 0), "time": b["time"]}
            order.append(key)
        else:
            agg = buckets[key]
            agg["high"] = max(agg["high"], b["high"])
            agg["low"] = min(agg["low"], b["low"])
            agg["close"] = b["close"]
            agg["volume"] += b.get("volume", 0)
    return [buckets[k] for k in order]


def simulate(bars: list[dict], *, channel: int = 20, vol_mult: float = 1.5,
             atr_mult: float = 2.0, atr_period: int = 14, slip_bps: float = 2.0,
             allow_short: bool = True) -> list[dict]:
    """Run the strategy over one symbol's 1h bars; return realized trades."""
    n = len(bars)
    if n < channel + atr_period + 1:
        return []
    H = [b["high"] for b in bars]
    L = [b["low"] for b in bars]
    C = [b["close"] for b in bars]
    V = [b.get("volume", 0) for b in bars]
    a = atr(bars, atr_period)
    slip = slip_bps / 10_000.0
    trades: list[dict] = []
    pos: dict | None = None

    for i in range(channel, n):
        dch_high = max(H[i - channel:i])
        dch_low = min(L[i - channel:i])
        avg_vol = sum(V[i - channel:i]) / channel
        vol_ok = avg_vol > 0 and V[i] >= vol_mult * avg_vol
        long_brk = C[i] > dch_high and vol_ok
        short_brk = C[i] < dch_low and vol_ok

        if pos is not None:
            exit_price = reason = None
            if pos["side"] == "long":
                stop = pos["extreme"] - atr_mult * pos["atr0"]
                if L[i] <= stop:
                    exit_price, reason = stop, "trail_stop"
                elif short_brk:
                    exit_price, reason = C[i], "reverse"
                else:
                    pos["extreme"] = max(pos["extreme"], H[i])
            else:
                stop = pos["extreme"] + atr_mult * pos["atr0"]
                if H[i] >= stop:
                    exit_price, reason = stop, "trail_stop"
                elif long_brk:
                    exit_price, reason = C[i], "reverse"
                else:
                    pos["extreme"] = min(pos["extreme"], L[i])
            if exit_price is not None:
                trades.append(_close(pos, exit_price, reason, bars[i]["date"], slip, atr_mult))
                pos = None

        if pos is None:
            if long_brk:
                pos = _open("long", i, C[i], a[i], H[i], bars[i]["date"])
            elif short_brk and allow_short:
                pos = _open("short", i, C[i], a[i], L[i], bars[i]["date"])

    if pos is not None:
        trades.append(_close(pos, C[-1], "eod", bars[-1]["date"], slip, atr_mult))
    return trades


def _open(side, i, price, atr0, extreme, date) -> dict:
    return {"side": side, "entry_idx": i, "entry_price": price, "atr0": atr0,
            "extreme": extreme, "entry_date": date}


def _close(pos, exit_price, reason, date, slip, atr_mult) -> dict:
    entry, side = pos["entry_price"], pos["side"]
    R = atr_mult * pos["atr0"] if pos["atr0"] > 0 else float("nan")
    if side == "long":
        entry_fill, exit_fill = entry * (1 + slip), exit_price * (1 - slip)
        ret = exit_fill / entry_fill - 1
        pnl_r = (exit_fill - entry_fill) / R
    else:
        entry_fill, exit_fill = entry * (1 - slip), exit_price * (1 + slip)
        ret = (entry_fill - exit_fill) / entry_fill
        pnl_r = (entry_fill - exit_fill) / R
    return {"side": side, "entry_date": pos["entry_date"], "exit_date": date,
            "entry_price": round(entry, 4), "exit_price": round(exit_price, 4),
            "exit_reason": reason, "ret": ret, "pnl_r": pnl_r,
            "bars_held": "n/a"}
