# Volume-Confirmed Donchian Breakout (1h, 2×ATR trail) — Backtest Report

Date: 2026-06-17
Runner: `backend/scripts/run_vol_donchian.py` · strategy
`app/strategies/breakout/volume_donchian.py` (tests `tests/test_volume_donchian.py`).
Universe: 10 stocks (TSLA NVDA AMD PLTR COIN MSTR META NFLX AMZN SMCI) + 2 indices (SPY, QQQ).
1-hour candles aggregated from cached intraday (stocks 5m→1h, indices 15m→1h), RTH,
2024-06→2026-06. 2 bp/side. Split IS<2025-06-05 / OOS>=2025-06-05.

Spec under test: 20-period high/low; break above the 20-high with volume ≥1.5× the 20-period
average volume → long; break below the 20-low with the same confirmation → short (and exit
long); 2×ATR trailing stop. Long + short, stop-and-reverse enabled. P&L in R (1R = initial
2×ATR risk); $ assumes 1% capital risk/trade.

## Results

| Metric | IS (older) | OOS (recent) |
|--------|-----------:|-------------:|
| Trades | 705 | 748 |
| Net $ (1% risk) | **+$2,560** | **+$42.63** |
| Win rate | 39.3% | 34.4% |
| Expectancy (R) | +0.182 | **+0.003** |
| Avg win / loss (R) | +1.28 / −0.53 | +1.07 / −0.55 |
| Profit factor | 1.566 | **1.008** |
| Symbols positive | 10/12 | 5/12 |

Exits were ~100% trailing-stop (the stop fired before an opposite-channel breakout, so the
reverse leg rarely engaged). % long ≈ 51% — balanced.

## Verdict: NO — strong in-sample, vanishes out-of-sample

This is the **classic breakout decay** seen before (daily Donchian: IS PF 3.08 → OOS 0.64).
Here it's less violent but the conclusion is the same: a great-looking in-sample (PF 1.57,
+0.18R/trade, 10/12 symbols green) collapses to a **coin flip out-of-sample** — expectancy
+0.003R (essentially zero), profit factor 1.01, only 5/12 symbols positive, net +$43 across
the whole year and universe. Not deployable.

## Why / notes

- **The volume filter and ATR trail did not create a durable edge.** They shape the trade
  distribution (low win rate ~34%, winners ~2× losers in R — a textbook trend profile) but
  the *expectancy* is what matters, and it decayed to zero OOS.
- **Cost-fragile by construction:** at OOS PF 1.008 the strategy is already at breakeven
  after 2 bp; any higher slippage pushes it negative. There is no margin.
- **No index edge:** SPY −$66 (loser), QQQ +$73 (winner) — mixed, consistent with "no real
  edge," same as the SPY/QQQ Bollinger study.
- **It's the opposite corner of the win-rate/RR tradeoff** from the 1:3 question: ~34% win
  at ~2:1 realized R. That can work *if* expectancy is positive — but here it isn't, OOS.

## Takeaway

Adds to the consistent project finding: **breakout/trend entries on this universe look
excellent in-sample and don't survive out-of-sample** (this one, daily Donchian, ORB+FVG).
The volume confirmation didn't change that. The only edges that have held up OOS remain
RSI-2 (risk-adjusted) and overnight drift (return). Standalone — not wired into the live app.
