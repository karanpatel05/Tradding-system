# Daily Anomaly Strategies — Backtest Report & Deploy Verdict

Date: 2026-06-16
Runner: `backend/scripts/run_daily_strategies.py` (RTH daily bars aggregated from `massive__*__5m.json`)
Universe: 10 watchlist names (TSLA NVDA AMD PLTR COIN MSTR META NFLX AMZN SMCI)
Capital $2,000 · commission $0 · 2bp/side slippage · range 2024-06-05 → 2026-06-04
**Split (honest convention):** tune on older year (IS, `<2025-06-05`); judge verdict on
most-recent held-out year (OOS, `>=2025-06-05`).

## Results

| Strategy | IS net | OOS net | OOS PF | OOS Sharpe | OOS maxDD | OOS sym+ | Verdict |
|----------|-------:|--------:|-------:|-----------:|----------:|:--------:|:-------:|
| **A1 Overnight (always-on)** | +$554.77 | **+$462.88** | 1.21 | 1.16 | −12.5% | 7/10 | **DEPLOYABLE** |
| A2 Overnight (close>SMA50)   | +$1,237.04 | +$265.23 | 1.13 | 0.72 | −20.4% | 7/10 | No (PF<1.2) |
| B Donchian (N=20, k=2.0)*    | +$496.45 | −$96.00 | 0.64 | −1.22 | −8.1% | 1/7 | No (overfit) |

\* best of a 3×3 sweep (N∈{10,20,55}, k∈{1.5,2,3}) selected on IS only.

## Benchmark — where does the return actually live? (OOS, gross, same 10 names)

| Leg (equal-weight) | OOS net | OOS TotRet | OOS Sharpe | OOS Sortino | OOS maxDD |
|--------------------|--------:|-----------:|-----------:|------------:|----------:|
| **Overnight** (close→open) | +$723 | **+36.1%** | **1.67** | 2.58 | **−11.1%** |
| Intraday (open→close)      | −$315 | −15.7% | −0.53 | −0.70 | −39.2% |
| Buy & hold (close→close)   | +$308 | +15.4% | 0.61 | 0.86 | −32.6% |

This is the core evidence that A1 is **not merely long beta**: holding these names 24h
(buy&hold) returned +15.4% at −33% drawdown; holding them *only overnight* returned
**+36.1% at −11% drawdown** — more than double the return, a third of the drawdown, ~2.7×
the Sharpe. The entire positive return came overnight; the **intraday session lost money
and carried all the risk**. That is a genuine risk-adjusted edge over the obvious
alternative, not a repackaging of the bull market. (These overnight figures are gross; the
deployable A1 line above is net of 2 bp/side.)

## Cost fragility — the decisive caveat (A1 at 5 bp/side)

| A1 Overnight | OOS net | OOS PF | OOS Sharpe | OOS sym+ |
|--------------|--------:|-------:|-----------:|:--------:|
| @ 2 bp/side  | +$462.88 | 1.21 | 1.16 | 7/10 |
| @ 5 bp/side  | +$118.55 | 1.07 | 0.39 | 4/10 |

The edge is thin and **cost-sensitive**: at 5 bp/side it still clears net>0 but FAILS the
robustness bar (PF<1.2, minority of symbols positive). The verdict is therefore
**conditional on execution cost ≈ ≤2 bp/side.** This is plausible — A1 trades the MOC and
MOO auctions, which are the most liquid prints of the day for these large names — but it
**must be confirmed against realized fills before committing real size.**

## Read

- **A1 Overnight drift — the winner.** Buy every name at the RTH close, sell at the next
  RTH open, equal-weight, every night. Positive in **both** periods (+27.7% IS / +23.1%
  OOS total return), OOS Sharpe 1.16 / Sortino 1.75, win rate ~56%, 7/10 symbols positive.
  This is the structural "overnight effect" and it is the **first strategy in this project
  to survive an honest out-of-sample test** (unlike ORB+FVG and the in-sample-only crossover
  optima). The edge is consistent, not period-specific.
- **Why A1 over A2 — the parameterless argument (not OOS-peeking).** On IS, A2 ($1,237)
  beats A1 ($554), so a naive "pick the best IS net" rule would choose A2 — which then fails
  OOS. The correct reason to prefer A1 is that **A1 has zero tuned parameters, so it cannot
  overfit**; its IS≈OOS consistency (+27.7% → +23.1%) is therefore trustworthy. A2 adds a
  tuned 50-day filter and shows exactly the IS→OOS decay (PF 1.56→1.13, Sharpe 2.37→0.72)
  that signals overfit. Prefer the parameterless strategy on principle; drop the filter.
- **B Donchian — classic overfit.** Gorgeous IS (PF 3.08, Sharpe 4.8) that completely
  inverts OOS (PF 0.64, net −$96, 1/7 symbols positive). Thin trade count (~30). Reject.

## Second search (2026-06-17): can we get to ≥30% annually?

Runner: `backend/scripts/run_overnight_boost.py`. Since leverage makes "hit 30%" trivial,
the objective is **robust OOS Sharpe** (lever the best to target, compare drawdown). Each
idea is one pre-committed config chosen on IS; OOS revealed once; leverage set on IS.

| Candidate | L\* (IS) | IS CAGR | OOS CAGR | OOS Sharpe | OOS maxDD | Result |
|-----------|-----:|-----:|-----:|-----:|-----:|:--|
| **A1 overnight (all, always)** | 1.12 | 30.0% | **25.0%** | **1.12** | −14.1% | reference |
| TOM overnight (turn-of-month) | 3.00 | −99.3% | 8.0% | 0.46 | −18.2% | **fails in-sample** |
| TopK overnight (IS top-5 Sharpe) | 1.00 | 68.6% | −0.9% | 0.06 | −18.3% | **selection overfit** |
| L/S long-overnight short-intraday | 3.00 | −86.1% | 19.9% | 0.67 | −73.4% | **fails IS; unstable** |

- **Nothing beat levered A1.** A1 has the highest robust OOS Sharpe (1.12).
- **TOM** (turn-of-month) overnight was *negative in-sample* — that seasonal does not exist
  on these names; rejected on IS without touching OOS.
- **TopK** (hold only the 5 best IS-Sharpe names: COIN META MSTR NFLX TSLA) gave a gorgeous
  IS (Sharpe 1.80) that collapsed OOS (Sharpe 0.06) — textbook selection overfit.
- **L/S market-neutral** was negative IS (its short-intraday leg loses in-sample because
  IS intraday was positive); the OOS "+20%" is the sign-unstable intraday leg, not edge, at
  a −73% drawdown. Rejected.

**Answer to "≥30% annually":** achievable only by **leveraging A1** (the same edge, not new
alpha). L≈1.12 set on IS reached 30% IS but 25% OOS; to realize ~30% *OOS* needs ≈1.35×,
which scales the drawdown to ~−17% and pushes the embedded 2bp slippage toward the 5bp
cost-fragility cliff. **The robust-alpha ceiling on these 10 names in this (bull) regime is
overnight drift, ~23–25%/yr.** 30% is a leverage/risk decision, not a better strategy.

## Verdict: is A1 worth deploying live?

**Qualified yes — contingent on execution cost, and understood as a risk-reduced beta tilt,
not a market-neutral edge.** The genuinely positive case: A1 cleared the robustness bar at
realistic cost (2 bp), held consistently across two years, is **parameterless (cannot
overfit)**, and — most importantly — **beats buy&hold of the same names on both return and
risk** (the intraday session is where the losses and drawdown live). That is a real edge.

But three caveats are load-bearing, two of them able to flip the call:

1. **Cost-fragile (decisive).** Deployable at ≤2 bp/side; at 5 bp it fails the bar. Do not
   commit size until realized MOC/MOO slippage on these names is measured. If fills run
   ≥4–5 bp, do not deploy.
2. **One regime only (decisive for risk).** The whole 2024-06→2026-06 window was a bull run
   for this universe. "Robust across two periods" really means *robust across two slices of
   the same uptrend.* A long, overnight-only book is precisely where a selloff hurts most —
   gap-downs happen overnight. Expect correlated overnight gap-downs in a drawdown; the
   −12.5% modeled OOS maxDD is a bull-market number, not a stress number.
3. **Thin & uneven.** OOS PF 1.21 barely clears; 7/10 names carried it. It is a small
   edge compounded over many bets, not a per-trade advantage.

**Recommendation:** worth a *small, cautiously-sized* live allocation **only after**
confirming execution cost ≈ ≤2 bp and accepting it as a beta-on diversifier that will draw
down with the market. Keep the SMA filter off (parameterless A1), add an earnings-night
exclusion per name, and monitor realized vs modeled slippage from day one. If you want
downside protection, the intraday-leg result suggests a natural hedge (the toxic leg is
intraday), but that is a separate design. Per the spec this is standalone and **not wired
into the live app** — wiring is the separately-gated next step.
