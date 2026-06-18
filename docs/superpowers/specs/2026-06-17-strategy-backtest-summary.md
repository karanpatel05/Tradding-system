# Strategy Backtest — Consolidated Report

Date: 2026-06-17
Universe: 10 watchlist names (TSLA NVDA AMD PLTR COIN MSTR META NFLX AMZN SMCI),
high-beta. Data: 2024-06-05 → 2026-06-04 (~2yr) from Massive 5m, aggregated as needed.
Capital $2,000 · commission $0 · default 2 bp/side slippage.
Validation (honest convention): tune on the older year (IS), judge on the most-recent
held-out year (OOS, `>=2025-06-05`).

## Verdict table (judged on OOS)

| Strategy | Type | OOS result | OOS Sharpe | OOS maxDD | Robust? |
|----------|------|-----------:|-----------:|----------:|:-------:|
| **Connors RSI-2** | mean reversion (daily) | **+12.5% CAGR** | **1.51** | **−4.5%** | ✅ best risk-adj; cost-robust |
| **Overnight drift (A1)** | overnight anomaly (daily) | **+23% CAGR** | 1.16 | −12.5% | ✅ but cost-fragile (dies @5bp) |
| Buy & hold (benchmark) | passive long | +15.5% CAGR | 0.61 | −32.6% | reference |
| ORB+FVG (day mode) | breakout+FVG (5m) | +$2,255 OOS / **−$832 IS** | — | — | ❌ failed generalization → dropped |
| ORB+FVG (scalp mode) | breakout+FVG (1m) | −$4,416 OOS | — | — | ❌ losing → dropped |
| Donchian breakout | trend-following (daily) | −$96 OOS, PF 0.64 | −1.22 | −8.1% | ❌ overfit (IS PF 3.08) |
| EMA 9/15 crossover* | momentum (intraday) | +$332 net, 8/10 sym | — | — | ⚠️ in-sample only (not OOS-validated) |

\* current live strategy; figures are the prior in-sample optimization, not the OOS regime
used for the rest of this table — not directly comparable.

> Note on comparability: RSI-2, A1, and buy&hold are equal-weight daily-return portfolios
> on the same $2k and ARE directly comparable (Sharpe/DD/CAGR). ORB+FVG and Donchian were
> measured as per-symbol $2k sleeves summed, so their dollar figures are not on the same
> basis — read them as pass/fail, not head-to-head dollars.

## What survived, what didn't

**Survived OOS (deployable):**
- **Connors RSI-2** — best risk-adjusted: Sharpe 1.51, drawdown just −4.5%, 74% win rate,
  trade PF 2.35, and **cost-robust** (5 bp barely changes it). In cash ~94% of the time.
  Lowest absolute return (12.5%). One clean OOS sample (SMA200 warmup eats the in-sample).
  Risk: no stop by design → tail risk untested in a real selloff.
- **Overnight drift A1** — highest robust return (~23%), beats buy&hold on return AND risk
  (the intraday session is where buy&hold's losses/drawdown live). But **cost-fragile**
  (fails at 5 bp) and it's long-only beta in one bull regime → gaps down in a selloff.

**Failed OOS (rejected):**
- **ORB+FVG** (both modes) — scalp lost outright; day mode made money in the older year but
  lost in the recent year (no generalization). Built, validated, **dropped** (code removed).
- **Donchian breakout** — gorgeous in-sample (PF 3.08) that inverted out-of-sample (PF 0.64).
  Textbook overfit.
- **The ≥30% search** (TOM seasonal, top-K selection, long/short market-neutral) — all
  failed in-sample or overfit. No new robust alpha beyond overnight drift on this universe.

## Recurring lesson

Across every test, in-sample performance was a poor guide: the strategies that looked best
in-sample (Donchian, ORB day, SMA-filtered overnight, top-K) were exactly the ones that
collapsed out-of-sample. The two that held up are the two with the least fitting —
parameterless overnight drift and Connors' published (un-tuned) RSI-2.

## Bottom line / decision

- Want **maximum profit / ≥30%**: only path is **leveraged A1 overnight** (~1.35×) — same
  edge, proportionally bigger drawdown (~−17%), and pushes toward the cost-fragility cliff.
- Want **best risk-adjusted, steady, cost-robust**: **Connors RSI-2** (12.5%, Sharpe 1.51,
  −4.5% DD) is the strongest find.
- Doing nothing new: the live **EMA crossover** has never been OOS-validated in this regime
  and should be, before trusting it.

All candidates are standalone and **not wired into the live app**. Per-strategy detail:
`2026-06-16-daily-anomaly-report.md` (A1, Donchian, ≥30% search),
`2026-06-17-connors-rsi2-report.md` (RSI-2), `2026-06-05-orb-fvg-*` / `2026-06-16-orb-fvg-oos-verdict.md` (ORB).
Runners: `scripts/run_daily_strategies.py`, `scripts/run_overnight_boost.py`, `scripts/run_rsi2.py`.
