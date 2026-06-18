# Connors RSI-2 Mean Reversion — Backtest Report & Verdict

Date: 2026-06-17
Runner: `backend/scripts/run_rsi2.py` · primitives `app/strategies/mean_reversion/rsi2.py`
(tests `tests/test_rsi2.py`). RTH daily bars from `massive__*__5m.json`, 10 names,
2024-06-05 → 2026-06-04. Capital $2,000 · equal-weight 10 sleeves · split 2025-06-05
(IS older / OOS recent, the honest convention).

Config (canonical, pre-committed — no OOS tuning): long-only; enter when `close > SMA200`
AND `RSI(2) < 10`; exit when `close > SMA5`; **no hard stop** (intrinsic to the method);
entry/exit at the close (MOC); 2 bp/side.

## Results

| | IS (older) | OOS (recent) |
|---|---:|---:|
| **Portfolio CAGR** | 5.7% | **12.5%** |
| Portfolio Sharpe | 1.18 | **1.51** |
| Portfolio Sortino | 3.47 | 2.36 |
| Portfolio max DD | −2.3% | **−4.5%** |
| Trades | 13 | 74 |
| Win rate | 84.6% | 74.3% |
| Trade profit factor | 28.9 | 2.35 |
| Avg win / avg loss | +5.0% / −1.0% | +3.7% / −4.5% |
| Symbols positive | 7/10 | 7/10 |

Market exposure **5.9%** (in cash ~94% of the time). **Cost-robust:** at 5 bp/side OOS
barely moves (CAGR 12.5%→12.0%, Sharpe 1.51→1.46) — multi-day holds make slippage trivial.

## First: this does NOT meet the "≥30% annually" goal

The request two steps ago was *more profit, ≥30% annually*. **RSI-2 returns 12.5% OOS — the
lowest absolute return of anything tested** (A1 overnight ~23%, buy&hold 15.5%). So against
that stated objective, RSI-2 *fails*. What it offers instead is the **best risk-adjusted
profile** found (highest Sharpe, smallest drawdown, cost-robust). The decision is a genuine
tradeoff the user must make: if max profit / 30% is still the goal, **leveraged A1 remains
the only path** (the same edge dialed up, with proportionally bigger drawdown); if the goal
shifts toward return-per-unit-risk, **RSI-2 is the best find.** Everything below is the
risk-adjusted case.

## Verdict: DEPLOYABLE on its own terms (best risk-adjusted strategy found)

On the held-out year RSI-2 cleared every bar: OOS net +$249, CAGR 12.5%, **Sharpe 1.51**,
trade PF 2.35, 7/10 symbols positive, max DD just −4.5%. Compared head-to-head on OOS:

| Strategy | OOS CAGR | OOS Sharpe | OOS maxDD | Cost-fragile? |
|----------|---------:|-----------:|----------:|:--:|
| **RSI-2 (this)** | 12.5% | **1.51** | **−4.5%** | no |
| A1 overnight drift | ~23% | 1.16 | −12.5% | yes (fails @5bp) |
| Buy & hold | 15.5% | 0.61 | −32.6% | n/a |

RSI-2 has the **highest Sharpe and by far the smallest drawdown**, and unlike A1 it is
**not cost-fragile**. Its headline CAGR (12.5%) is modest precisely *because* it sits in
cash ~94% of the time — and that low drawdown (−4.5%) is a **product of that low exposure**,
not free headroom. Scaling deployment to use the idle capital (concentrating per signal)
would lift return **and drawdown together** — re-introducing exactly the per-name, no-stop,
high-beta tail flagged below. You can raise CAGR or keep the tiny DD, not both. On a
risk-adjusted basis as-run, it is the best thing we have tested.

## Caveats (load-bearing)

1. **One clean sample, not two.** The params (RSI 2 / entry 10 / exit SMA5 / trend SMA200)
   are Connors' *published canonical* values — not fit to this data — so the positive OOS
   year is a valid no-overfit test (the same parameterless logic that vindicated A1). The
   real limit is just that the SMA200 warmup leaves only **13 IS trades** (valid signal
   doesn't start until ~April 2025), so there is **no independent in-sample corroboration** —
   it is one clean held-out sample, not two. Strong, but less cross-validated than ideal.
2. **No-stop tail risk.** The method has no stop by design; OOS avg loss (−4.5%) already
   exceeds avg win (+3.7%) — it wins by frequency (74%), not by cutting losers. A high-beta
   name that keeps falling after an RSI-2 entry can produce a large single loss; the −4.5%
   portfolio DD is a one-(bull)-year number and understates true tail risk.
3. **One regime.** As with everything here, the held-out year is still one bull-ish regime;
   mean reversion typically does *well* in chop/selloffs, so this is less worrying than for
   the long-only beta strategies, but it is unproven in a real bear market.

## Recommendation

RSI-2 is the **strongest risk-adjusted candidate so far** and worth advancing — but it is
*not* the answer to "≥30% annually" (see top). Before sizing up: (a) get more history to
build a real independent in-sample (the current IS is mostly warmup); (b) decide a
catastrophe-stop or max-hold rule to cap the no-stop tail, accepting it will dent the edge;
(c) treat any move to use the idle 94% of capital as a deliberate **return-for-drawdown
trade**, not a free lunch. If the user holds firm on 30%, the honest recommendation is
leveraged A1, not RSI-2. Standalone — **not wired into the live app**; wiring is the
separately-gated next step.
