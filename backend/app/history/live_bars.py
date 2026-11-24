"""Live-trade recording helpers: the configured stock list and trade-to-1-minute folding."""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Iterable, NamedTuple
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.chart_data import ChartDataError, normalize_symbol
from app.services.market import _is_trading_day

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_MINUTE_MS = 60_000
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)
_INDEX_SYMBOLS = {"SPX", "NDX"}


class Trade(NamedTuple):
    symbol: str
    ts_ms: int
    seq: int          # DXLink TimeAndSale.index, unique per event
    price: float
    size: float
    valid: bool = True  # TimeAndSale.valid_tick


def stock_symbols() -> tuple[str, ...]:
    out: list[str] = []
    for raw in get_settings().live_bar_symbols.split(","):
        if not raw.strip():
            continue
        try:
            symbol = normalize_symbol(raw)
        except ChartDataError:
            logger.warning("LIVE_BAR_SYMBOLS: dropping invalid entry %r", raw)
            continue
        if symbol in _INDEX_SYMBOLS:
            logger.warning("LIVE_BAR_SYMBOLS: dropping index symbol %s", symbol)
            continue
        if symbol not in out:
            out.append(symbol)
    return tuple(out)


def _et_parts(ts_ms: int) -> tuple[datetime, int]:
    return datetime.fromtimestamp(ts_ms // 1000, ET), ts_ms % 1000


def is_rth_trade(ts_ms: int, extended: bool | None) -> bool:
    """Session filter only: 09:30:00.000-16:00:00.000 ET inclusive, on a trading day."""
    if extended:
        return False
    dt, ms = _et_parts(ts_ms)
    if not _is_trading_day(dt.date()):
        return False
    t = dt.time()
    if t < _RTH_OPEN:
        return False
    return t < _RTH_CLOSE or (t == _RTH_CLOSE and ms == 0)


def minute_of(ts_ms: int) -> int:
    """Start of the trade's minute in ms; the 16:00:00.000 ET closing print belongs to 15:59."""
    minute = ts_ms - ts_ms % _MINUTE_MS
    dt, ms = _et_parts(ts_ms)
    if dt.time() == _RTH_CLOSE and ms == 0:
        minute -= _MINUTE_MS
    return minute


class _Bar(dict):
    """A plain bar dict plus price bookkeeping that is not part of its items."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.first_ts = 0
        self.last_ts = 0
        self.has_valid = False


def fold(bars: dict[int, dict], trades: Iterable[Trade]) -> set[int]:
    """Fold trades into 1-minute bars in trade-time order; returns the touched minute keys.

    Prices come from valid trades only (or from invalid ones until the minute's first valid
    trade arrives); volume and `trades` count every trade."""
    touched: set[int] = set()
    for tr in sorted(trades, key=lambda t: (t.ts_ms, t.seq)):
        m = minute_of(tr.ts_ms)
        bar = bars.get(m)
        if bar is None:
            bar = bars[m] = _Bar(time=m, open=tr.price, high=tr.price, low=tr.price,
                                 close=tr.price, volume=0.0, trades=0)
            bar.first_ts = bar.last_ts = tr.ts_ms
            bar.has_valid = tr.valid
        elif tr.valid and not bar.has_valid:
            bar.update(open=tr.price, high=tr.price, low=tr.price, close=tr.price)
            bar.first_ts = bar.last_ts = tr.ts_ms
            bar.has_valid = True
        elif tr.valid or not bar.has_valid:
            bar["high"] = max(bar["high"], tr.price)
            bar["low"] = min(bar["low"], tr.price)
            if tr.ts_ms < bar.first_ts:
                bar["open"], bar.first_ts = tr.price, tr.ts_ms
            if tr.ts_ms >= bar.last_ts:
                bar["close"], bar.last_ts = tr.price, tr.ts_ms
        bar["volume"] += float(tr.size)
        bar["trades"] += 1
        touched.add(m)
    return touched
