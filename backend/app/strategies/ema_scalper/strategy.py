"""EmaScalperStrategy — strategy_1.

Ties the pure pieces together behind the project's ``BaseStrategy`` interface:
``scan`` builds the daily watchlist, ``backtest_returns`` replays history through
the shared signal core, and ``live_decision`` runs that same core on the latest
closed bar for the live worker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.strategies.base import BaseStrategy, Candidate
from app.strategies.ema_scalper.params import EmaScalperParams
from app.strategies.ema_scalper.runner import (
    is_past_cutoff,
    is_session_open,
    regime_now,
    regime_series,
    session_series,
)
from app.providers import get_historical_provider
from app.services import history_cache
from app.strategies.ema_scalper.scanner import build_watchlist
from app.strategies.ema_scalper.selection import aggregate_selection, pass_days, select_daily_trades
from app.strategies.ema_scalper.signals import Action, State, generate_signal, replay

logger = logging.getLogger("trading.ema_scalper")

# Daily lookback for the scanner (enough for trend EMA + RVOL window).
_DAILY_LOOKBACK_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
# Intraday lookback — DXFeed caps 5-min history; we take whatever it returns.
_INTRADAY_LOOKBACK_START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _bar_date(ms: int | None):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _iso_dt(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _iso_date(ms: int | None) -> str | None:
    d = _bar_date(ms)
    return d.isoformat() if d else None


def _trade_dict(symbol: str, t) -> dict:
    """Project a signals.Trade to the JSON shape the trade-history table consumes."""
    return {
        "symbol": symbol,
        "date": _iso_date(t.exit_time),
        "side": t.side,
        "entry_type": t.entry_type,
        "entry_time": _iso_dt(t.entry_time),
        "exit_time": _iso_dt(t.exit_time),
        "entry_price": round(t.entry_price, 4),
        "exit_price": round(t.exit_price, 4),
        "stop": round(t.stop, 4),
        "target": round(t.target, 4),
        "return_pct": round(t.return_pct, 6),
        "r_multiple": round(t.r_multiple, 3),
        "reason": t.reason,
    }


class EmaScalperStrategy(BaseStrategy):
    key = "strategy_1"
    name = "9-15 EMA Scalping"
    ready = True
    is_rule_driven = True

    description = (
        "Scans the stock universe daily and identifies high-probability "
        "trend-following pullback setups using the 9 EMA and 15 EMA on 5-minute "
        "candles. Uses EMA trend confirmation, an objective EMA-slope strength "
        "filter (EMA15 linear regression vs ATR), pullback entries with candlestick "
        "confirmation, and fixed 1:2 risk/reward management. Flat by the session "
        "cutoff — no overnight holds."
    )
    rules = [
        "Execution timeframe: 5-minute candles. Indicators: EMA 9, EMA 15, ATR(14).",
        "Scanner: price ≥ $10, avg volume ≥ 1M, RVOL ≥ 2, gap ≥ 2%, ATR ≥ 2% (premarket-volume filter optional).",
        "Bullish trend: EMA9 > EMA15, price above EMA15, EMA15 slope positive and strong; bearish is the mirror.",
        "Slope strength: EMA15 linear-regression slope over 10 candles ≥ 0.20·ATR (long) / ≤ −0.20·ATR (short) — objective, no visual angles.",
        "Sideways reject: stand down when |EMA9 − EMA15| < 0.15·ATR (flat/chop structure).",
        "Long entries — (1) pullback into EMA zone + bullish pin bar, (2) + bullish engulfing, (3) close below zone then strong reversal back above both EMAs. Enter on break of that candle's high. Shorts mirror.",
        "Entry validation: reject when |price − EMA15| > 2·ATR (no chasing extended moves).",
        "Stop just beyond the entry candle (± 0.10·ATR buffer, configurable); target at fixed 1:2 reward-to-risk.",
        "Position size = floor(allocated capital ÷ entry price), whole shares; reject if one share costs more than the allocation.",
        "Flat all positions by the session cutoff (default 3:45 PM ET); no overnight holds.",
    ]
    entry_logic = (
        "Trend confirmed (EMA9 vs EMA15, price the right side of EMA15, EMA15 LR-slope "
        "≥ 0.20·ATR) + not sideways (|EMA9−EMA15| ≥ 0.15·ATR) + pullback into the EMA "
        "zone with a typed confirmation candle (pin / engulfing / reversal) + within "
        "2·ATR of EMA15 → stop entry at the candle's high (long) / low (short)."
    )
    exit_logic = (
        "Stop beyond the entry candle (± 0.10·ATR), fixed 1:2 target, early exit on a "
        "sharp opposite reversal candle, and a hard flat at the session cutoff."
    )
    selection_criteria = (
        "Daily scan of the universe with liquidity (price ≥ $10, avg vol ≥ 1M), "
        "in-play (RVOL ≥ 2), volatility (ATR ≥ 2%) and gap (≥ 2%) filters, plus a "
        "trend gate (price the right side of a rising/falling longer EMA). Ranked by a "
        "0–100 score weighting relative volume, ATR%, gap and trend distance; the "
        "highest-scoring valid setup is selected."
    )

    def __init__(self) -> None:
        self.params = EmaScalperParams.from_settings()

    # --- resilience hooks ------------------------------------------------
    def bar_interval(self) -> str:
        return self.params.candle_interval

    def backfill_specs(self, selected_symbol: str | None) -> list[tuple[str, str]]:
        return [(selected_symbol, self.params.candle_interval)] if selected_symbol else []

    # --- scanner ---------------------------------------------------------
    async def _fetch_daily(self, symbol: str) -> list[dict]:
        # Historical data comes from IBKR (cached to disk, namespaced by source).
        provider = get_historical_provider()
        return await history_cache.get_or_fetch(
            symbol, "1d",
            lambda: provider.get_bars(symbol, "1d", start=_DAILY_LOOKBACK_START),
            source=provider.name,
        )

    async def scan(self, universe: list[str]) -> list[Candidate]:
        return await build_watchlist(universe, self._fetch_daily, self.params)

    # --- intraday data ---------------------------------------------------
    async def fetch_intraday(self, symbol: str) -> list[dict]:
        interval = self.params.candle_interval
        provider = get_historical_provider()
        bars = await history_cache.get_or_fetch(
            symbol, interval,
            lambda: provider.get_bars(symbol, interval, start=_INTRADAY_LOOKBACK_START),
            source=provider.name,
        )
        for b in bars:
            b.setdefault("symbol", symbol)
        return bars

    # --- backtest (replay the shared core) -------------------------------
    async def backtest_returns(self, symbol: str, params: EmaScalperParams | None = None) -> dict:
        """Replay ``symbol``'s 5-min history through the exact ``generate_signal``
        the live worker uses; return dated returns + detailed per-trade records."""
        p = params or self.params
        bars = await self.fetch_intraday(symbol)
        out: dict = {
            "symbol": symbol,
            "interval": p.candle_interval,
            "dated_returns": [],
            "trades": [],
            "trade_count": 0,
            "date_range": {"start": None, "end": None, "bars": len(bars)},
            "note": None,
        }
        if len(bars) < p.warmup_bars + 5:
            out["note"] = "Not enough intraday history returned for this symbol."
            return out

        regime_bars = await self.fetch_intraday(p.regime_symbol) if p.regime_filter else []
        allow_long, allow_short = regime_series(bars, regime_bars, p)
        can_open, must_flat = session_series(bars, p)
        trades = replay(
            bars, p,
            allow_long=allow_long, allow_short=allow_short,
            can_open=can_open, must_flat=must_flat,
            slippage_pct=p.slippage_pct,
        )
        dated = [(_bar_date(t.exit_time), t.return_pct) for t in trades if t.exit_time]
        out["dated_returns"] = [(d.isoformat(), r) for d, r in dated if d]
        out["trades"] = [_trade_dict(symbol, t) for t in trades]
        out["trade_count"] = len(trades)
        out["date_range"] = {
            "start": _iso_date(bars[0].get("time")),
            "end": _iso_date(bars[-1].get("time")),
            "bars": len(bars),
        }
        if not trades:
            out["note"] = "No qualifying setups in the available history."
        return out

    # --- universe scan (shared by backtest + selection history) ----------
    async def _gather_universe(self, universe: list[str], params: EmaScalperParams,
                               on_progress=None) -> list[dict]:
        """Per-symbol analyses for the universe: each symbol's as-of daily scanner
        scores (``pass_days``) and its first trade per day from the continuous
        intraday replay (``trades_by_day``). Heavy on the first run (fetches the
        universe's history); cached after. One bad symbol is skipped, not fatal.

        ``on_progress(scanned, total, symbol)`` (optional) is invoked after each
        symbol so callers can stream progress; it fires even for skipped symbols."""
        analyses: list[dict] = []
        total = len(universe)
        for i, symbol in enumerate(universe, start=1):
            try:
                sim = await self.backtest_returns(symbol, params)
                daily = await self._fetch_daily(symbol)
                trades_by_day: dict[str, dict] = {}
                for t in sim.get("trades", []):
                    day = t.get("date")
                    if day and day not in trades_by_day:  # first (only) trade that day
                        trades_by_day[day] = t
                analyses.append({"symbol": symbol, "pass_days": pass_days(daily, params),
                                 "trades_by_day": trades_by_day})
            except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't fail the scan
                logger.debug("universe scan skipped %s: %s", symbol, exc)
            if on_progress:
                on_progress(i, total, symbol)
        return analyses

    # --- universe daily-selection backtest -------------------------------
    async def backtest_daily_selection(self, universe: list[str],
                                       params: EmaScalperParams | None = None,
                                       start: str | None = None,
                                       end: str | None = None,
                                       on_progress=None) -> dict:
        """Backtest the way the strategy actually trades live: each day pick the
        highest-scoring scanner stock that formed a setup (best-of-watchlist) and
        take that one trade. Returns the selected trades (full dicts, chronological)
        plus the covered date range — fed to the shared trade summarizer.
        ``on_progress`` is forwarded to the per-symbol universe scan."""
        p = params or self.params
        analyses = await self._gather_universe(universe, p, on_progress=on_progress)
        trades = select_daily_trades(analyses, start, end)
        out: dict = {
            "trades": trades,
            "universe_size": len(universe),
            "date_range": {"start": None, "end": None, "trading_days": 0},
            "note": None,
        }
        if not analyses:
            out["note"] = ("No cached history yet. Sign in so candle data is cached for the "
                           "universe, then re-run — the daily scan reads from the local cache.")
            return out
        if not trades:
            out["note"] = "No qualifying setups across the universe for this period."
            return out
        out["date_range"] = {
            "start": trades[0]["date"],
            "end": trades[-1]["date"],
            "trading_days": len(trades),
        }
        return out

    # --- daily selection history (universe scan transparency) ------------
    async def selection_history(self, universe: list[str], start: str | None = None,
                                end: str | None = None, capital: float = 2000.0,
                                params: EmaScalperParams | None = None) -> dict:
        """Per-day scan→select→outcome history across the universe.

        Reuses the per-symbol continuous backtest (same ``generate_signal``) for
        trades and the daily scanner for as-of scores, then ranks by score per day.
        Heavy on the first run (fetches the universe's history); cached after."""
        p = params or self.params
        analyses = await self._gather_universe(universe, p)
        rows = aggregate_selection(len(universe), analyses, capital, start, end)
        note = None
        if not rows:
            note = ("No cached history yet. Sign in and run a backtest so candle data is "
                    "cached for the universe, then reload — the per-day scan reads from the "
                    "local cache.")
        return {"days": rows, "universe_size": len(universe), "note": note}

    # --- live decision (same core, latest bar) ---------------------------
    async def live_decision(self, symbol: str, state: State) -> tuple[Action | None, list[dict]]:
        """Run ``generate_signal`` on the freshly-closed bar for ``symbol``."""
        p = self.params
        bars = await self.fetch_intraday(symbol)
        if len(bars) < p.warmup_bars:
            return None, bars
        regime_bars = await self.fetch_intraday(p.regime_symbol) if p.regime_filter else []
        allow_long, allow_short = regime_now(regime_bars, p)
        latest_ms = bars[-1].get("time")
        action = generate_signal(
            bars, state, p,
            allow_long=allow_long, allow_short=allow_short,
            can_open=is_session_open(latest_ms, p),
            must_flat=is_past_cutoff(latest_ms, p),
        )
        return action, bars
