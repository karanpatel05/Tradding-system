"""EmaCrossoverStrategy — the multi-position 9/15 EMA crossover (strategy_1).

Ties the pure crossover signal core to the project's ``BaseStrategy`` interface.
Unlike the single-position scalper this strategy is **multi-position**: it trades a
fixed, configurable watchlist concurrently (one position per symbol) and is driven
by the multi-position worker loop rather than the scan→select-one loop. The same
the shared ``signals.decide_index`` powers both the live ``live_decision`` (latest
closed bar) and the event-driven portfolio backtest, so they can't diverge — and
the backtest reuses the live ``RiskEngine`` for sizing parity too.

Historical bars come from Massive (cached to disk); the optional 15m HTF trend
gate is computed from the same provider. Risk sizing / portfolio limits live in
:mod:`app.engine.risk` and are applied by the worker, not here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.providers import get_historical_provider
from app.services import history_cache
from app.strategies.base import BaseStrategy, Candidate
from app.strategies.ema_crossover.params import EmaCrossoverParams
from app.strategies.ema_crossover.signals import (
    Action, State, Trade, direction_gates, generate_signal,
)
from app.strategies.ema_scalper import indicators as ind

logger = logging.getLogger("trading.ema_crossover")

_INTRADAY_LOOKBACK_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
_DAILY_LOOKBACK_START = datetime(2024, 1, 1, tzinfo=timezone.utc)


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


def _in_range(d: str | None, start: str | None, end: str | None) -> bool:
    """Inclusive ISO-date window check; an undated row is excluded."""
    if not d:
        return False
    return (start is None or d >= start) and (end is None or d <= end)


def _trade_dict(symbol: str, t: Trade) -> dict:
    """Project a signals.Trade to the JSON shape the backtest summarizer consumes."""
    return {
        "symbol": symbol,
        "date": _iso_date(t.exit_time),
        "side": t.side,
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


def _portfolio_trade_dict(pt, slippage_per_side_bps: float = 0.0) -> dict:
    """Project a portfolio.PortfolioTrade (risk-sized) to the summarizer's JSON
    shape. Carries the engine-sized ``shares``/``pnl``/``capital_used`` so the
    summarizer reports actual risk-fractional sizing (parity with live).

    ``slippage_per_side_bps`` (>0) deducts a per-side fill cost so every cost-derived
    metric is **net of cost**. It is **order-type-aware**: the live bracket exits a
    ``target`` via a LIMIT order (fills at price → no slippage), while the entry and
    any stop/crossover/EOD exit are MARKET orders that cross the spread. So the entry
    leg is always charged and the exit leg only when it isn't a ``target``. Commission
    is $0 (Tastytrade equities). Default 0 keeps the single-backtest path cost-free."""
    t = pt.trade
    d = _trade_dict(t.symbol, t)
    d.update({
        "shares": pt.shares,
        "capital_used": round(pt.capital_used, 2),
        "pnl": round(pt.pnl, 2),
        "hold_minutes": round(pt.hold_minutes, 1) if pt.hold_minutes is not None else None,
    })
    if slippage_per_side_bps:
        slip = slippage_per_side_bps / 10_000.0
        charge_exit = t.reason != "target"  # target = limit fill, no slippage
        cost_per_share = slip * t.entry_price + (slip * t.exit_price if charge_exit else 0.0)
        d["pnl"] = round(pt.pnl - cost_per_share * pt.shares, 2)
        d["return_pct"] = round(t.return_pct - slip * (2.0 if charge_exit else 1.0), 6)
        risk = abs(t.entry_price - t.stop)
        d["r_multiple"] = round(t.r_multiple - (cost_per_share / risk if risk else 0.0), 3)
    return d


def _bar_ms(bars: list[dict]) -> int:
    """Bar duration in ms = smallest positive gap between consecutive open times.
    Derived from the data so it works for any interval and is robust to overnight
    gaps (which produce large diffs we ignore via the min)."""
    diffs = [b2["time"] - b1["time"]
             for b1, b2 in zip(bars, bars[1:])
             if b1.get("time") is not None and b2.get("time") is not None]
    pos = [d for d in diffs if d > 0]
    return min(pos) if pos else 0


def _htf_states(htf_bars: list[dict], params: EmaCrossoverParams) -> list[tuple[int, bool, bool]]:
    """(close_time, allow_long, allow_short) per HTF bar: long allowed when the HTF
    fast EMA is above the slow EMA, short when below. Warmup bars don't block.

    The state is tagged with the bar's CLOSE time (open + interval), not its open
    time, so an intraday bar only adopts an HTF bar's gate once that HTF bar has
    fully closed — otherwise the gate would read EMA values built from a close up to
    one HTF interval in the future (look-ahead). ``_htf_now`` (live) discards this
    time field, so the retag is backtest-only and never shifts a live decision."""
    closes = [b["close"] for b in htf_bars]
    ef = ind.ema(closes, params.htf_fast)
    es = ind.ema(closes, params.htf_slow)
    dur = _bar_ms(htf_bars)
    out: list[tuple[int, bool, bool]] = []
    for i, b in enumerate(htf_bars):
        close_ms = (b.get("time") or 0) + dur
        if ef[i] is None or es[i] is None:
            out.append((close_ms, True, True))
            continue
        out.append((close_ms, ef[i] > es[i], ef[i] < es[i]))
    return out


def _htf_series(intraday_bars: list[dict], htf_bars: list[dict],
                params: EmaCrossoverParams) -> tuple[list[bool], list[bool]]:
    """Per-(intraday)bar HTF gates aligned by timestamp — each intraday bar uses the
    most recent HTF bar at-or-before its time (no lookahead). Backtest counterpart
    to the live ``_htf_now``, so the gate is computed identically in both."""
    n = len(intraday_bars)
    if not params.use_htf_filter or not htf_bars:
        return [True] * n, [True] * n
    states = _htf_states(htf_bars, params)
    allow_long, allow_short = [True] * n, [True] * n
    j = 0
    for i, b in enumerate(intraday_bars):
        t = b.get("time") or 0
        while j + 1 < len(states) and states[j + 1][0] <= t:
            j += 1
        if states and states[j][0] <= t:
            allow_long[i], allow_short[i] = states[j][1], states[j][2]
    return allow_long, allow_short


class EmaCrossoverStrategy(BaseStrategy):
    key = "strategy_1"
    name = "9-15 EMA Crossover"
    ready = True
    is_rule_driven = True
    is_multi_position = True

    description = (
        "Trades a configurable watchlist with a 9/15 EMA crossover on 5-minute "
        "candles, stock-only, long and short, holding multiple positions at once "
        "(one per symbol). Entries require a fresh EMA9/EMA15 crossover confirmed "
        "by price beyond both EMAs, a volume surge, and aligned EMA slopes; an "
        "optional 15-minute higher-timeframe trend filter gates direction. "
        "Position sizing and portfolio exposure are governed by a risk engine "
        "(1% risk/trade, capped by max positions, aggregate risk, and a daily-loss "
        "halt)."
    )
    rules = [
        "Execution timeframe: 5-minute candles. Indicators: EMA 9, EMA 15, ATR(14).",
        "Universe: a fixed, configurable watchlist (one concurrent position per symbol).",
        "Bullish entry: EMA9 crosses above EMA15, close above both EMAs, volume > "
        "multiplier × average volume, and both EMA slopes positive. Bearish mirrors.",
        "Optional 15m higher-timeframe filter: long only when HTF EMA-fast > EMA-slow; "
        "short only when below.",
        "Exit: opposite EMA crossover, the ATR-based stop, or the fixed reward-to-risk target.",
        "Stop = stop_atr_mult × ATR from entry; target = risk_reward × the per-share risk.",
        "Risk sizing: shares = floor(equity × risk_per_trade_pct ÷ per-share risk), then "
        "capped by the shared buying-power pool across open positions.",
        "Portfolio limits: max concurrent positions, max aggregate open risk, a daily-loss "
        "halt, and a per-day trade cap — entries stand down when any limit is hit.",
    ]
    entry_logic = (
        "Flat in a symbol → a fresh EMA9/EMA15 crossover in the allowed direction, with "
        "close beyond both EMAs, a volume surge (> multiplier × average), and EMA slopes "
        "signed the same way (and the optional HTF trend agreeing) → enter at the close; "
        "stop = stop_atr_mult × ATR, target at the fixed reward-to-risk."
    )
    exit_logic = (
        "Exit on the opposite EMA crossover, a touch of the ATR stop, or the reward-to-risk "
        "target — whichever comes first. Positions are not force-flat at the session close."
    )
    selection_criteria = (
        "Operates on a fixed configurable watchlist rather than a daily scan. Each symbol "
        "is eligibility-screened for price, liquidity (avg volume), in-play activity "
        "(relative volume) and volatility (ATR%); the watchlist is traded concurrently."
    )

    def __init__(self) -> None:
        self.params = EmaCrossoverParams.from_settings()

    # --- data ------------------------------------------------------------
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

    async def _fetch_htf(self, symbol: str) -> list[dict]:
        if not self.params.use_htf_filter:
            return []
        interval = self.params.htf_interval
        provider = get_historical_provider()
        return await history_cache.get_or_fetch(
            symbol, interval,
            lambda: provider.get_bars(symbol, interval, start=_INTRADAY_LOOKBACK_START),
            source=provider.name,
        )

    async def _fetch_daily(self, symbol: str) -> list[dict]:
        provider = get_historical_provider()
        return await history_cache.get_or_fetch(
            symbol, "1d",
            lambda: provider.get_bars(symbol, "1d", start=_DAILY_LOOKBACK_START),
            source=provider.name,
        )

    async def _fetch_1m(self, symbol: str) -> list[dict]:
        """Native 1-minute bars — used ONLY for higher-fidelity intrabar stop/target
        fill resolution in the backtest (not for signals, which stay on 5m)."""
        provider = get_historical_provider()
        bars = await history_cache.get_or_fetch(
            symbol, "1m",
            lambda: provider.get_bars(symbol, "1m", start=_INTRADAY_LOOKBACK_START),
            source=provider.name,
        )
        for b in bars:
            b.setdefault("symbol", symbol)
        return bars

    # --- eligibility scan (satisfies BaseStrategy; not used by the worker) -
    async def scan(self, universe: list[str]) -> list[Candidate]:
        """Eligibility-screen the watchlist on price/liquidity/in-play/volatility.

        The multi-position worker trades the watchlist directly and does NOT call
        this; it exists to satisfy the interface and to power read-only views."""
        watchlist = get_settings().watchlist_symbols
        p = self.params
        out: list[Candidate] = []
        for symbol in watchlist:
            try:
                daily = await self._fetch_daily(symbol)
            except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't fail the scan
                logger.debug("crossover scan skipped %s: %s", symbol, exc)
                continue
            if len(daily) < max(p.atr_length, p.vol_avg_window) + 2:
                continue
            price = daily[-1]["close"]
            vols = [b.get("volume") or 0.0 for b in daily]
            avg_vol = sum(vols[-p.vol_avg_window:]) / p.vol_avg_window
            rel_vol = (vols[-1] / avg_vol) if avg_vol else 0.0
            atr = ind.atr(daily, p.atr_length)[-1] or 0.0
            atr_pct = (atr / price) if price else 0.0
            if (price >= p.min_price and avg_vol >= p.min_avg_volume
                    and rel_vol >= p.min_rel_volume and atr_pct >= p.min_atr_pct):
                score = round(min(1.0, rel_vol / 5.0) * 0.5 + min(1.0, atr_pct / 0.1) * 0.5, 4)
                out.append(Candidate(
                    symbol=symbol, score=score,
                    reason=f"RVOL {rel_vol:.1f}, ATR {atr_pct*100:.1f}%",
                    metrics={"price": round(price, 2), "rel_volume": round(rel_vol, 2),
                             "atr_pct": round(atr_pct, 4), "avg_volume": int(avg_vol)},
                ))
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    # --- HTF gate --------------------------------------------------------
    def _htf_now(self, htf_bars: list[dict]) -> tuple[bool, bool]:
        """Latest HTF gate (live). (allow_long, allow_short)."""
        p = self.params
        if not p.use_htf_filter or len(htf_bars) <= p.htf_slow:
            return True, True
        _, allow_long, allow_short = _htf_states(htf_bars, p)[-1]
        return allow_long, allow_short

    # --- live decision (same core, latest bar) ---------------------------
    async def live_decision(self, symbol: str, state: State,
                            direction: str = "both") -> tuple[Action | None, list[dict]]:
        """Run ``generate_signal`` on the freshly-closed bar for ``symbol``.
        ``direction`` gates which sides may OPEN (long/short/both) — AND-combined
        with the HTF trend gate via the same ``direction_gates`` the backtest uses.

        The RTH session gate is applied identically to the backtest (``session_flags``
        on the bar's timestamp): entries only within the session window, and any open
        position is force-flat at/after the close — so live and backtest can't diverge."""
        p = self.params
        bars = await self.fetch_intraday(symbol)
        if len(bars) < p.warmup_bars:
            return None, bars
        htf_long, htf_short = self._htf_now(await self._fetch_htf(symbol))
        allow_long, allow_short = direction_gates(direction, htf_long, htf_short)
        # RTH session gating (entry window + EOD force-flat) is applied inside
        # decide_index — the single funnel — so live and backtest can't diverge.
        action = generate_signal(
            bars, state, p,
            allow_long=allow_long, allow_short=allow_short,
            can_open=True, must_flat=False, external_fills=True,
        )
        return action, bars

    # --- shared universe fetch (backtest + optimizer) --------------------
    async def _fetch_universe_bars(self, symbols: list[str], p: EmaCrossoverParams,
                                   on_progress=None, *, with_1m: bool = True) -> tuple[dict, dict, dict]:
        """Fetch signal (5m), HTF (15m) and — when ``with_1m`` — native 1m bars for
        each symbol ONCE. Used by both the portfolio backtest and the optimizer so a
        sweep fetches once / sims many (and every combo sees identical bars). 1m is
        for intrabar fill resolution only; signals stay on native 5m. One bad symbol
        is skipped. Returns (bars_by_symbol, htf_by_symbol, bars_1m_by_symbol)."""
        bars_by_symbol: dict[str, list[dict]] = {}
        htf_by_symbol: dict[str, tuple[list[bool], list[bool]]] = {}
        bars_1m_by_symbol: dict[str, list[dict]] = {}
        total = len(symbols)
        for idx, symbol in enumerate(symbols, start=1):
            try:
                bars = await self.fetch_intraday(symbol)
                if len(bars) >= p.warmup_bars + 5:
                    # When the HTF filter is on, the symbol must have HTF bars or it
                    # would be backtested UNGATED — silently diverging from how it
                    # trades live. Drop it instead of trading it without the gate.
                    htf_series = None
                    if p.use_htf_filter:
                        htf_bars = await self._fetch_htf(symbol)
                        if not htf_bars:
                            logger.debug("HTF data unavailable for %s — excluded from "
                                         "HTF-on backtest (would otherwise run ungated)", symbol)
                            continue
                        htf_series = _htf_series(bars, htf_bars, p)
                    bars_by_symbol[symbol] = bars
                    if htf_series is not None:
                        htf_by_symbol[symbol] = htf_series
                    if with_1m:
                        one_min = await self._fetch_1m(symbol)
                        if one_min:
                            bars_1m_by_symbol[symbol] = one_min
            except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't fail the run
                logger.debug("universe fetch skipped %s: %s", symbol, exc)
            finally:
                if on_progress:
                    on_progress(idx, total, symbol)
        return bars_by_symbol, htf_by_symbol, bars_1m_by_symbol

    # --- parameter optimization (grid sweep, fetch-once/sim-many) ---------
    async def optimize(self, symbols: list[str], *, grid: dict | None = None,
                       directions: list[str] | None = None, objective: str = "net_profit",
                       capital: float = 2000.0, start: str | None = None,
                       end: str | None = None, slippage_per_side_bps: float = 0.0,
                       on_progress=None) -> dict:
        """Grid-search the crossover params over the watchlist, ranked by ``objective``.
        Fetches each symbol's history ONCE, then re-runs the in-memory sim for every
        grid combo × direction (same simulator/metrics as the backtest)."""
        from app.backtest.optimizer import (
            DEFAULT_DIRECTIONS, DEFAULT_GRID, combo_count, optimize_grid, validate_grid,
        )
        from app.engine.risk import RiskLimits

        p = self.params
        grid = validate_grid(grid) if grid else dict(DEFAULT_GRID)
        directions = directions or list(DEFAULT_DIRECTIONS)
        out: dict = {
            "results": [], "universe_size": len(symbols), "objective": objective,
            "combos": combo_count(grid, directions), "grid": grid,
            "directions": directions, "slippage_per_side_bps": slippage_per_side_bps,
            "note": None,
        }
        bars_by_symbol, htf_by_symbol, bars_1m = await self._fetch_universe_bars(symbols, p, on_progress)
        if not bars_by_symbol:
            out["note"] = ("No cached history yet. Sign in so candle data is cached for the "
                           "watchlist, then re-run the optimizer.")
            return out
        out["results"] = optimize_grid(
            bars_by_symbol, htf_by_symbol or None, p, RiskLimits.from_settings(), capital,
            grid=grid, directions=directions, objective=objective, start=start, end=end,
            watchlist_order=symbols, bars_1m_by_symbol=bars_1m or None,
            slippage_per_side_bps=slippage_per_side_bps)
        if not out["results"]:
            out["note"] = "No combinations produced results."
        return out

    # --- portfolio backtest (multi-position, risk-sized — the live model) -
    async def backtest_portfolio(self, symbols: list[str],
                                 params: EmaCrossoverParams | None = None, *,
                                 direction: str = "both",
                                 start: str | None = None, end: str | None = None,
                                 capital: float = 2000.0, on_progress=None) -> dict:
        """Backtest the way the strategy trades live: the whole watchlist run
        together through one shared buying-power pool, risk-sized by the same risk
        engine (sizing parity), long/short/both via ``direction``. Heavy on the
        first run (fetches each symbol's history); cached after. ``start``/``end``
        filter the *reported* trades by exit date — the pool is never reset mid-run.
        """
        from app.backtest.portfolio import simulate_portfolio
        from app.engine.risk import RiskLimits

        p = params or self.params
        bars_by_symbol, htf_by_symbol, bars_1m = await self._fetch_universe_bars(symbols, p, on_progress)

        out: dict = {
            "trades": [],
            "universe_size": len(symbols),
            "direction": direction,
            "fills": "1m" if bars_1m else "5m",
            "date_range": {"start": None, "end": None, "trading_days": 0},
            "note": None,
        }
        if not bars_by_symbol:
            out["note"] = ("No cached history yet. Sign in so candle data is cached for the "
                           "watchlist, then re-run — the backtest reads from the local cache.")
            return out

        sim_trades = simulate_portfolio(
            bars_by_symbol, p, RiskLimits.from_settings(), equity=capital,
            direction=direction, htf_by_symbol=htf_by_symbol or None,
            watchlist_order=symbols, bars_1m_by_symbol=bars_1m or None,
        )
        rows = [_portfolio_trade_dict(pt) for pt in sim_trades]
        rows.sort(key=lambda r: (r["exit_time"] or "", r["entry_time"] or ""))
        if start or end:
            rows = [r for r in rows if _in_range(r.get("date"), start, end)]

        out["trades"] = rows
        if not rows:
            out["note"] = "No qualifying setups across the watchlist for this period."
            return out
        days = sorted({r["date"] for r in rows if r.get("date")})
        out["date_range"] = {"start": days[0] if days else None,
                             "end": days[-1] if days else None,
                             "trading_days": len(days)}
        return out
