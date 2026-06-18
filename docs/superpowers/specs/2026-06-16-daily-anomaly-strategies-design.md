# Daily Anomaly Strategies — Backtest & Deploy Verdict

Date: 2026-06-16
Status: Approved — implement standalone, report only; do NOT wire live regardless of result.

## Goal
Find a profitable **new** strategy (distinct from the live 9/15 EMA crossover and the
dropped ORB+FVG) on cached data, judged by **balanced robustness**: out-of-sample net
profit, positive across a majority of symbols, acceptable profit factor / drawdown.
Two candidates: (A) overnight drift, (B) Donchian breakout.

## Data layer (shared)
No Massive daily cache exists. Build clean **RTH daily bars** by aggregating
`massive__SYMBOL__5m.json` per ET date: `open` = first 09:30 bar open, `close` = last
bar with ET time <= 16:00 close, `high`/`low` = extremes over RTH bars only (drop
pre/post-market). 10 symbols (TSLA NVDA AMD PLTR COIN MSTR META NFLX AMZN SMCI),
coverage 2024-06-05 → 2026-06-04. Capital $2,000; commission $0.

## Validation split — corrected convention
Unlike the ORB spec (which labeled OOS = older year), use the **honest deploy
convention**: tune/select on the **older year** (2024-06-05 → 2025-06-04, in-sample),
judge the verdict on the **most-recent held-out year** (2025-06-05 → 2026-06-04, OOS).
"Would this have made money going forward." For B's parameter sweep, tune on IS only,
then evaluate the single locked config on OOS with no peeking.

## Strategy A — Overnight drift
Equal-weight portfolio. Each night, hold each eligible symbol from RTH close to next
RTH open; per-symbol overnight return = open[t+1]/close[t] − 1. Daily portfolio return
= mean across eligible symbols. Two variants compared:
1. **always-on** (hold every symbol every night),
2. **regime-filtered** (hold a symbol only when its close[t] > its 50-day SMA).
No per-trade stop; sizing is equal-weight on capital. Costs: 2 bp/side slippage applied
to each overnight round-trip (enter at close, exit at open).

## Strategy B — Donchian breakout
Long-only (shorting high-beta breakouts is a trap). Per symbol on RTH daily bars:
- **Entry:** close[t] > max(high[t−N .. t−1]) → enter long at open[t+1] (market).
- **Stop:** entry − k × ATR(14). **Exit** on stop, or on close < min(low[t−N/2 .. t−1]),
  whichever first; else mark-to-exit at end of data.
- **Sizing:** risk-fractional, shares = floor(capital × 1% / (entry − stop)), capped by
  capital. Costs: 2 bp/side slippage on market legs.
- **Sweep (IS only):** N ∈ {10, 20, 55}, k ∈ {1.5, 2, 3}. Lock best-by-IS-net, evaluate on OOS.

## Metrics & verdict
Reuse `app/backtest/metrics.py:summarize` where it fits (return-series for A; per-trade
returns for B). Report per period (IS, OOS): net profit, CAGR, Sharpe, Sortino, profit
factor, win rate, max drawdown, total trades, and **% of symbols positive**.
**Deployable verdict (balanced robustness):** OOS net > 0 AND positive in a majority of
symbols AND profit factor ≳ 1.2 AND drawdown not catastrophic. Anything short = report
why, do not deploy.

## Build
`app/strategies/daily_anomaly/` — pure logic: `rth_daily` (aggregation), `overnight`
(sim), `donchian` (sim + ATR). Runner `scripts/run_daily_strategies.py`. TDD the
primitives first (deterministic, synthetic bars). Standalone — NOT wired into the live
app; the report is the deliverable.

## Notes
Repo not under git; spec on disk, not committed.
