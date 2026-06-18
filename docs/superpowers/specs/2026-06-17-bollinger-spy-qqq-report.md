# Bollinger Intraday Mean Reversion (SPY/QQQ, 15m) — Backtest Report

Date: 2026-06-17
Runner: `backend/scripts/run_bollinger.py` · strategy `app/strategies/mean_reversion/bollinger.py`
(tests `tests/test_bollinger.py`). Data: SPY & QQQ 15m pulled fresh from Massive (Polygon),
RTH only (12,960 bars each), 2024-06-18 → 2026-06-17. 2 bp/side slippage.

Spec under test: 20-period SMA + population σ on 15m candles. Long when close < mean −
num_std·σ; short when close > mean + num_std·σ; exit when close returns to the mean.
SPY 1.5σ, QQQ 1.8σ. No stop is specified by the method. Two exit regimes reported because
the spec doesn't state one: **literal** (hold to the mean, even overnight) and **intraday**
(force-flat at each day's close). Split: IS `<2025-06-05` / OOS `>=2025-06-05`.

## Results

| Config | IS return | OOS return | OOS win% | OOS PF | Verdict |
|--------|----------:|-----------:|---------:|-------:|:-------:|
| SPY 1.5σ literal | −15.0% | −12.2% | 57.4% | 0.82 | ❌ |
| SPY 1.5σ intraday | −14.6% | −15.6% | 51.2% | 0.77 | ❌ |
| QQQ 1.8σ literal | **+10.7%** | −15.0% | 56.9% | 0.82 | ❌ |
| QQQ 1.8σ intraday | −1.6% | −18.8% | 50.6% | 0.77 | ❌ |

## Verdict: NOT profitable — all four configs lose out-of-sample

The strategy as specified does not work on SPY/QQQ 15m over this 2-year window. Every
variant loses money in the held-out year (−12% to −19%), and only one config (QQQ literal)
was even positive in-sample — and it *flipped to −15% OOS*, the textbook overfit/regime tell.

## Why it fails (and it's instructive)

This is the **classic mean-reversion trap**, and it ties directly to the 1:3 study from
earlier today:

- **The win rate is high — 50–57% OOS — exactly the kind of number that looks attractive.**
  But the strategy still loses, because **average loss is ~1.5–2× average win** (SPY OOS:
  +0.31% win vs −0.51% loss). Price reverts *most* of the time (the frequent small wins),
  but when it doesn't — a trend day — the no-stop position runs against you for a large loss
  that erases many wins. High win rate, negative expectancy.
- **Costs make a marginal edge negative.** Avg gross win is only ~0.3–0.4%; a 4 bp
  round-trip (2 bp × 2) eats ~10–13% of that. Even gross, profit factor would sit ~0.85–0.95
  — still losing — so costs aren't the root cause, the win/loss asymmetry is.
- **A stop wouldn't obviously save it:** the method's edge *is* the reversion; cutting losers
  early (which a stop does) tends to remove the trades that would have reverted, and prior
  tests here show forcing fixed brackets onto reversion entries hurts them.

## "Do the opposite to be profitable?" — tested (mode=breakout)

Flipping the entries (long above +σ, short below −σ, still exit at the mean) does **not**
work — it loses *more*:

| Config (OOS) | Reversion | Breakout (opposite) |
|--------------|----------:|--------------------:|
| SPY literal  | −12.2% (PF 0.82, win 57%) | **−32.0%** (PF 0.54, win 35%) |
| SPY EOD-flat | −15.6% (PF 0.77) | −32.7% (PF 0.54) |
| QQQ literal  | −15.0% (PF 0.82, win 57%) | **−26.3%** (PF 0.63, win 41%) |
| QQQ EOD-flat | −18.8% (PF 0.77) | −25.3% (PF 0.64) |

Why the mirror isn't a winner:
1. **Costs are paid both ways.** The reversion side was only *mildly* losing gross (~PF 0.90
   before the 4 bp round-trip). Its mirror is ~breakeven gross — then pays the same costs
   again → still a loser. You can't flip a cost-driven loss into a profit.
2. **The breakout side also fights the (real but small) intraday reversion.** Index ETFs do
   revert mildly at the bands — that's why reversion wins 57% of the time. Chasing the
   breakout bets *against* that tendency **and** pays costs, so it loses on both counts: win
   rate drops to 35–41% and profit factor to 0.54–0.64, far worse than the fade.
3. **~3× the trades** (SPY breakout 1,100 vs reversion 319) → ~3× the cost drag.

**Conclusion: neither side is profitable.** There is no exploitable edge at the ±σ bands on
SPY/QQQ 15m net of costs — both fading and chasing just pay the spread; fading is merely
"less bad." Flipping a losing strategy is not a path to profit when the loss is driven by
costs and the absence of an edge.

## Trend filter — does fading *with* the higher-timeframe trend rescue it?

Added a directional filter: only take long fades when close > an N-bar trend SMA, only
short fades when below it (drops counter-trend fades — the source of the big losers). Tested
N ∈ {100, 200, 390} bars, literal exit, judged on both periods (not cherry-picked by OOS).

| Config | IS ret | OOS ret | OOS PF |
|--------|-------:|--------:|-------:|
| SPY no filter | −15.0% | −12.2% | 0.82 |
| SPY trend=200 | +1.8% | −3.1% | 0.90 |
| QQQ no filter | +10.7% | −15.0% | 0.82 |
| **QQQ trend=100** | +6.7% | **+9.9%** | **1.43** |
| QQQ trend=200 | +12.0% | +0.7% | 1.03 |

**The filter works as designed** — it removes the big counter-trend losers and roughly
halves-to-eliminates the loss everywhere. Two real outcomes:
- **SPY:** improves from −12% to near breakeven (−3%), but **never turns positive** at any
  trend length. No edge.
- **QQQ:** actually flips **positive in both periods** for short trend filters (trend=100:
  +6.7% IS / +9.9% OOS, PF 1.43; trend=200 marginally positive). This is the first
  index result in the black.

### …but it's cost-fragile (the decisive check)

| QQQ config | OOS @2bp | OOS @5bp |
|------------|---------:|---------:|
| trend=100 literal | +9.9% (PF 1.43) | **+2.8% (PF 1.12)** |
| trend=100 EOD-flat | +6.6% (PF 1.34) | −0.4% (PF 0.99) |
| trend=200 literal | +0.7% (PF 1.03) | −6.1% (PF 0.81) |

Only **one** config (QQQ, trend=100, literal, low slippage) stays positive under a 5 bp
stress, and even it drops to PF 1.12 — below the 1.2 robustness bar. Everything else dies.

### Verdict on the trend filter

A clear *improvement* but **not a robust edge.** It exposes a faint mean-reversion tendency
on QQQ, but the edge is **about the size of transaction costs**: profitable at 2 bp,
marginal-to-negative at 5 bp, and it **does not generalize to SPY** (which stays negative
throughout). So whether it makes money depends entirely on execution quality, and it works
on one of two near-identical instruments — both hallmarks of an edge that isn't really there.
Better than the raw fade (clearly losing), but not a green light to deploy.

## Takeaway

Bollinger-band fade on index ETFs at 15m is a widely-shared idea that does not survive an
honest backtest on recent data — same lesson as ORB+FVG, Donchian, and the 1:3 study: a
plausible, popular rule with a nice win rate, undone by negative expectancy out-of-sample.
The validated edges on this project remain RSI-2 (risk-adjusted) and overnight drift
(return). SPY/QQQ 15m data is now cached for any future tests. Standalone — nothing wired
into the live app.
