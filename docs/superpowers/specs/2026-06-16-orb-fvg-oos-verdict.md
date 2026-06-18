# ORB+FVG — OOS Validation Verdict

Date: 2026-06-16
Runner: `backend/scripts/run_orb_fvg.py` (reads `data_cache/massive__*__{1m,5m}.json`)
Universe: 10 watchlist symbols (TSLA NVDA AMD PLTR COIN MSTR META NFLX AMZN SMCI)
Capital $2,000 · 1% risk/trade · 2bp slippage on market legs · 2yr data 2024-06-05→2026-06-04
Split: 2025-06-05. **Spec labels OOS = older year (< boundary), IS = recent year (>= boundary).**

## Results (net after costs)

| Mode  | OOS (2024-06→2025-06) | IS (2025-06→2026-06) | Both years |
|-------|----------------------:|---------------------:|-----------:|
| Scalp (1m) | **-$4,416** (PF 0.92, 9,619 tr) | -$7,079 (PF 0.83) | **-$11,495** |
| Day (5m)   | **+$2,255** (PF 1.11, 3,334 tr) | -$832 (PF 0.95) | **+$1,423** |

## Verdict

- **Scalp: FAIL.** Loses in both periods, PF < 1 throughout, win rate ~26%. Dead — drop it.
- **Day: passes the literal spec test (OOS net > 0) but FAILS generalization.** It made money
  in the older year and *lost* money in the most recent year (2025-06→2026-06) — the period we'd
  actually trade going forward. PF only 1.11 → 0.95, win rate 35-38%. Net across both years is
  a thin +$1,423 over 2 years on $2k × 10 symbols.

## Decision

Per the spec, integration is gated on OOS profitability AND robustness review. Day mode is a
**marginal candidate, not a clear pass**: the edge did not hold in the most recent year, so it
is **NOT wired into the live app**. Recommended next step before any integration: investigate
why day mode decays in the recent year (regime? specific symbols?) and/or a parameter sweep,
rather than deploying as-is.
