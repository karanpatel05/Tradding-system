# ORB + FVG Strategy — Backtest & Validation (pre-integration)

Date: 2026-06-05
Status: Approved — implement standalone, integrate only if OOS-profitable.

## Goal
Implement an Opening-Range-Breakout + Fair-Value-Gap strategy (two modes) as a
**standalone backtest** on our cached data, judged by **out-of-sample** net profit
after realistic costs. Integrate into the live app ONLY if it passes OOS.

## Common engine
- Per symbol, per day, daily reset. Opening range recomputed each day; force-flat
  all positions at 16:00 ET; no new entries after 15:30 ET. Direction long+short.
  Multiple setups/day (re-arm after each trade/invalidation).
- Sizing: existing RiskEngine, $2k, 1% risk/trade. Costs: commission $0; slippage
  default 2 bp/side on MARKET legs only (limit fills free). Fills on the execution
  timeframe OHLC, stop wins ties.
- Validation: in-sample (≥2025-06-05) vs out-of-sample (<2025-06-05). Verdict = OOS.

## Scalping mode (1-min)
1. Opening range = first 5-min candle (09:30–09:35 ET) → opening_high/low.
2. Breakout = a 1-min candle closes beyond the range.
3. FVG = 3 consecutive 1-min candles in breakout direction, forming at/after breakout
   (most recent qualifying FVG tracked). Bullish FVG: c1.high < c3.low, zone
   [c1.high, c3.low]. Bearish: c1.low > c3.high, zone [c3.high, c1.low].
4. Entry: price retraces into the FVG zone, then a bullish engulfing 1-min candle
   (close>prev_open & open<prev_close & close>open) → enter long at its close (market).
   Short mirrors with bearish engulfing.
5. Stop = lowest low during the retrace inside the FVG − buffer (2 ticks = $0.02).
   Target = entry + 3×risk (limit). RR 3:1.

## Day-trading mode (5-min)
1. Opening range = first 15-min candle (09:30–09:45 ET).
2. Breakout = 5-min candle closes beyond the range.
3. FVG = 3 consecutive 5-min candles in breakout direction after breakout.
4. Entry: resting limit at 50% of the FVG (midpoint); fills if a later 5-min candle
   trades through it; cancels at session end if unfilled.
5. Stop = below FVG candle-1 low (long) / above candle-1 high (short).
   Target = entry + 2×risk (limit). RR 2:1.

## Metrics (both modes, IS + OOS)
Net Profit, CAGR, Sharpe, Sortino, Profit Factor, Win Rate, Avg Win, Avg Loss,
Max Drawdown, Total Trades.

## Build
Standalone `app/strategies/orb_fvg/` (pure logic: opening_range, detect_fvg,
is_engulfing, breakout, per-day simulator) + a runner script. TDD the primitives
first. NOT wired into the live app until it proves out OOS.

## Notes
Repo not under git; spec on disk, not committed.
