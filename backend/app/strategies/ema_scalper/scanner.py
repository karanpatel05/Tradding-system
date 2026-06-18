"""Daily watchlist scanner.

Builds a ranked, dynamic watchlist of high-volatility trending names instead of a
fixed ticker list. The scoring (:func:`score_symbol`) is a pure function of a
symbol's daily bars, so it runs identically whether the bars come from the live
feed (today) or are sliced as-of a historical morning in backtest — no lookahead
beyond the as-of bar the caller passes in.

Filters (all configurable):
* **Volatility / in-play:** ATR(14)/price ≥ ``min_atr_pct``.
* **Relative volume:** last session volume / average ≥ ``min_rel_volume``.
* **Momentum:** price trending the same side as a rising/falling longer EMA.
* **Liquidity floors:** average volume ≥ ``min_avg_volume`` and price ≥ ``min_price``.

NOTE (honest limit): the universe (``engine.universe.SEED_UNIVERSE``) is today's
liquid names, so a historical watchlist reconstructed from it is survivorship-
biased. The signal backtest on a chosen symbol is bias-free; the historical
*watchlist* is approximate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.strategies.base import Candidate
from app.strategies.ema_scalper import indicators as ind
from app.strategies.ema_scalper.params import EmaScalperParams

logger = logging.getLogger("trading.ema_scanner")

# An async callable: symbol -> list of daily OHLC bars (oldest→newest, as-of).
DailyFetch = Callable[[str], Awaitable[list[dict]]]

_RVOL_WINDOW = 20  # sessions used for average volume / relative volume


def score_symbol(daily_bars: list[dict], params: EmaScalperParams) -> Candidate | None:
    """Score one symbol from its daily bars, or None if it fails any filter."""
    need = max(params.trend_ema_length, params.atr_length, _RVOL_WINDOW) + 2
    if len(daily_bars) < need:
        return None

    closes = [b["close"] for b in daily_bars]
    vols = [b.get("volume") or 0 for b in daily_bars]
    price = closes[-1]
    if price < params.min_price:
        return None

    window_vols = vols[-(_RVOL_WINDOW + 1):-1]  # exclude today from the average
    avg_vol = sum(window_vols) / len(window_vols) if window_vols else 0.0
    if avg_vol < params.min_avg_volume:
        return None

    atr_series = ind.atr(daily_bars, params.atr_length)
    atr_val = atr_series[-1]
    if not atr_val or price <= 0:
        return None
    atr_pct = atr_val / price
    if atr_pct < params.min_atr_pct:
        return None

    rel_vol = (vols[-1] / avg_vol) if avg_vol else 0.0
    if rel_vol < params.min_rel_volume:
        return None

    # Gap: today's open vs the prior session close.
    prev_close = closes[-2]
    gap_pct = abs(daily_bars[-1]["open"] / prev_close - 1.0) if prev_close else 0.0
    if gap_pct < params.min_gap_pct:
        return None
    # Premarket-volume floor is opt-in: standard daily bars don't carry premarket
    # volume, so when enabled without that data a symbol simply can't qualify.
    if params.use_premarket_filter and not daily_bars[-1].get("premarket_volume"):
        return None
    if params.use_premarket_filter and daily_bars[-1].get("premarket_volume", 0) < params.min_premarket_volume:
        return None

    trend_series = ind.ema(closes, params.trend_ema_length)
    trend_ema = trend_series[-1]
    trend_prev = trend_series[-1 - params.slope_lookback]
    if trend_ema is None or trend_prev is None:
        return None
    ema_rising = trend_ema > trend_prev
    ema_falling = trend_ema < trend_prev

    if price > trend_ema and ema_rising:
        direction = "long"
    elif price < trend_ema and ema_falling:
        direction = "short"
    else:
        return None  # not a clean trend

    distance = abs(price / trend_ema - 1.0)
    # Score (0-100-ish) prioritises in-play volume, volatility, gap and trend.
    score = round(min(rel_vol * atr_pct * (1.0 + distance + gap_pct) * 1000, 100.0), 2)
    return Candidate(
        symbol=daily_bars[-1].get("symbol", ""),
        score=score,
        reason=f"{direction} trend, ATR {atr_pct*100:.1f}%, RVOL {rel_vol:.1f}x, gap {gap_pct*100:.1f}%",
        metrics={
            "direction": direction,
            "atr_pct": round(atr_pct, 4),
            "rel_volume": round(rel_vol, 2),
            "gap_pct": round(gap_pct, 4),
            "avg_volume": round(avg_vol, 0),
            "price": round(price, 2),
            "trend_ema": round(trend_ema, 2),
        },
    )


async def build_watchlist(symbols: list[str], fetch_daily: DailyFetch,
                          params: EmaScalperParams) -> list[Candidate]:
    """Score the universe concurrently and return the ranked top-N watchlist.

    ``fetch_daily(symbol)`` supplies as-of daily bars; a symbol whose data is
    missing or fails a filter is simply dropped (best-effort)."""

    async def one(sym: str) -> Candidate | None:
        try:
            bars = await fetch_daily(sym)
        except Exception as exc:  # noqa: BLE001
            logger.debug("scanner fetch failed for %s: %s", sym, exc)
            return None
        for b in bars:  # stamp the symbol so score_symbol can label the candidate
            b.setdefault("symbol", sym)
        return score_symbol(bars, params)

    scored = await asyncio.gather(*(one(s) for s in symbols))
    ranked = sorted((c for c in scored if c is not None), key=lambda c: c.score, reverse=True)
    return ranked[: params.watchlist_size]
