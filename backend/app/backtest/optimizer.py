"""Grid-search optimizer for the EMA crossover.

A deterministic parameter sweep: for every combination of grid values × direction
it runs the **same** ``simulate_portfolio`` the backtest uses, summarizes it with
the **same** ``_summarize_portfolio``, and ranks the combos by a chosen objective.
Deterministic grid (not a stochastic/Bayesian search) so results are reproducible
and testable.

Critical performance/correctness property: the optimizer is **pure over already-
fetched bars** — the caller fetches each symbol's history ONCE and passes
``bars_by_symbol``/``htf_by_symbol`` in; we only ``dataclasses.replace`` the params
and re-run the (fast, in-memory) sim. Re-fetching per combo would be slow *and*
unfair — a cache TTL expiry mid-run could make combos see different bars and skew
the ranking.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

from app.backtest.engine import _summarize_portfolio
from app.backtest.portfolio import simulate_portfolio
from app.strategies.ema_crossover.strategy import _in_range, _portfolio_trade_dict

# Param fields the grid may sweep (whitelist — guards dataclasses.replace + the API).
ALLOWED_GRID_FIELDS = frozenset({
    "risk_reward", "stop_atr_mult", "vol_multiplier", "fast", "slow",
    "slope_lookback", "atr_length", "vol_avg_window",
})
# Metrics that may be used as the ranking objective (all higher-is-better, incl.
# max_drawdown which is ≤ 0 so "less negative" sorts higher).
ALLOWED_OBJECTIVES = frozenset({
    "net_profit", "net_return_pct", "sharpe", "profit_factor", "win_rate",
    "expectancy", "avg_r_multiple", "max_drawdown", "trade_count",
})
_INT_FIELDS = frozenset({"fast", "slow", "slope_lookback", "atr_length", "vol_avg_window"})

# Default grid when the caller doesn't specify one.
DEFAULT_GRID: dict[str, list] = {
    "risk_reward": [1.5, 2.0, 2.5, 3.0],
    "stop_atr_mult": [1.0, 1.5, 2.0],
    "vol_multiplier": [1.2, 1.5, 2.0],
}
DEFAULT_DIRECTIONS = ["long", "short", "both"]
MAX_COMBOS = 600  # guard against a runaway sweep

_EMPTY_METRICS = {
    "net_profit": 0.0, "net_return_pct": 0.0, "sharpe": 0.0, "profit_factor": None,
    "win_rate": 0.0, "expectancy": 0.0, "avg_r_multiple": 0.0, "max_drawdown": 0.0,
    "avg_hold": 0.0,
}


def validate_grid(grid: dict[str, list]) -> dict[str, list]:
    """Whitelist + coerce a grid spec; raises ValueError on unknown fields, empty
    value lists, or non-numeric values. Returns a cleaned grid."""
    clean: dict[str, list] = {}
    for field, values in (grid or {}).items():
        if field not in ALLOWED_GRID_FIELDS:
            raise ValueError(f"unknown grid field: {field!r}")
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"grid field {field!r} needs a non-empty list of values")
        coerced = []
        for v in values:
            try:
                coerced.append(int(v) if field in _INT_FIELDS else float(v))
            except (TypeError, ValueError):
                raise ValueError(f"grid field {field!r} has a non-numeric value: {v!r}")
        clean[field] = coerced
    return clean


def _expand(grid: dict[str, list]) -> list[dict]:
    """Cartesian product of the grid → list of param-override dicts ({} if empty)."""
    if not grid:
        return [{}]
    fields = list(grid)
    return [dict(zip(fields, combo)) for combo in itertools.product(*(grid[f] for f in fields))]


def combo_count(grid: dict[str, list], directions: list[str]) -> int:
    return len(_expand(grid)) * max(1, len(directions))


def _objective_value(metrics: dict, objective: str, trade_count: int):
    if objective == "trade_count":
        return trade_count
    return metrics.get(objective)


def optimize_grid(bars_by_symbol: dict[str, list[dict]], htf_by_symbol, base_params,
                  limits, equity: float, *, grid: dict[str, list], directions: list[str],
                  objective: str = "net_profit", start: str | None = None,
                  end: str | None = None, watchlist_order: list[str] | None = None,
                  bars_1m_by_symbol: dict[str, list[dict]] | None = None,
                  slippage_per_side_bps: float = 0.0,
                  on_combo=None) -> list[dict]:
    """Sweep ``grid`` × ``directions`` over the pre-fetched bars and return combos
    ranked best-first by ``objective``. ``start``/``end`` filter each combo's
    reported trades by exit date (report-only; the pool is never reset).
    ``bars_1m_by_symbol`` (optional) gives every combo the same 1m intrabar fill
    resolution the backtest uses."""
    if objective not in ALLOWED_OBJECTIVES:
        raise ValueError(f"unknown objective: {objective!r}")
    combos = _expand(grid)
    directions = directions or ["both"]
    total = len(combos) * len(directions)
    if total > MAX_COMBOS:
        raise ValueError(f"grid too large: {total} combinations (max {MAX_COMBOS})")

    results: list[dict] = []
    n = 0
    for direction in directions:
        for override in combos:
            params = replace(base_params, **override) if override else base_params
            trades = simulate_portfolio(
                bars_by_symbol, params, limits, equity, direction=direction,
                htf_by_symbol=htf_by_symbol, watchlist_order=watchlist_order,
                bars_1m_by_symbol=bars_1m_by_symbol)
            rows = [_portfolio_trade_dict(pt, slippage_per_side_bps) for pt in trades]
            if start or end:
                rows = [r for r in rows if _in_range(r.get("date"), start, end)]
            metrics = _summarize_portfolio(rows, equity)["metrics"] if rows else dict(_EMPTY_METRICS)
            results.append({
                "params": override,
                "direction": direction,
                "objective": objective,
                "objective_value": _objective_value(metrics, objective, len(rows)),
                "trade_count": len(rows),
                "metrics": metrics,
            })
            n += 1
            if on_combo:
                on_combo(n, total, override, direction)

    results.sort(key=_rank_key, reverse=True)
    for rank, r in enumerate(results, start=1):
        r["rank"] = rank
    return results


def _rank_key(r: dict) -> tuple[bool, float]:
    """Ranking key (sorted desc). Two degeneracies handled explicitly:

    * **Zero-trade combos rank below every combo that traded.** A param set that
      never triggers isn't a deployable winner regardless of metric — without this,
      ``max_drawdown`` (0.0 for no trades vs negative for real ones) would crown the
      param set that does nothing.
    * **``profit_factor`` is ``None`` only when there were no losses** — that's the
      *best* possible, so it maps to ``+inf`` (for a trading combo), not ``-inf``."""
    has_trades = r["trade_count"] > 0
    v = r["objective_value"]
    if v is None:
        v = float("inf") if has_trades else float("-inf")
    return (has_trades, v)
