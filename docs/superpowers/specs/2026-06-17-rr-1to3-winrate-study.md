# 1:3 Risk-Reward Win-Rate Study

Date: 2026-06-17
Runner: `backend/scripts/run_rr_test.py` · harness `app/strategies/rr_test/bracket.py`
(tests `tests/test_rr_bracket.py`). 10 names, RTH daily, 2024-06→2026-06, 2bp/side.
Identical bracket on every entry: **stop = 1×ATR(14), target = 3×ATR(14)** (1:3), long-only,
enter next open, stop wins ties. Question: can any entry deliver **~50% win at 1:3**?
(Breakeven win rate at 1:3 is 25%; 50% would be +1.0R/trade.)

## Results — OOS (held-out recent year, the verdict)

| Entry | Win% | Target% | Stop% | Expectancy (R) | Profit factor |
|-------|-----:|--------:|------:|---------------:|--------------:|
| **RSI-2 oversold** (reversion) | **32.4%** | 28.4% | 67.6% | **+0.20** | 1.30 |
| Donchian-20 breakout | 23.9% | 21.7% | 76.1% | −0.10 | 0.87 |
| EMA 9/15 cross (momentum) | 17.5% | 17.5% | 82.5% | −0.30 | 0.64 |

In-sample for reference: EMA 38.2%, RSI-2 61.5% (only **13 trades** — SMA200 warmup),
Donchian 34.7%.

## Findings

1. **None achieved 50% win at 1:3 out-of-sample.** Best was RSI-2 at **32.4%**. The only
   reading above 50% anywhere (RSI-2 IS, 61.5%) was the **13-trade warmup sample**, and it
   collapsed to 32.4% on the real OOS year — exactly the small-sample mirage that makes the
   "holy grail" look reachable and isn't.
2. **At 1:3, win rates cluster 18–32%** — right around the 25% breakeven, and consistent
   with the ORB scalp result (26.6%) we already had. This is the empirical reality on these
   names: demanding a 3:1 target means winning roughly a quarter to a third of the time.
3. **Two of three LOSE money at 1:3** (negative R-expectancy): momentum (EMA) and breakout
   (Donchian) both fall *below* the 25% breakeven, so the 3:1 target is a net drag.
4. **Forcing 3:1 onto RSI-2 makes it worse, not better.** RSI-2's natural small-target exit
   (close>SMA5) had profit factor **2.35**; the same entry with a 3:1 target drops to **1.30**.
   High-win-rate entries have *small* natural edges — stretching the target to 3:1 destroys
   the win rate faster than it grows the winners.

## Verdict

**A 50%-win / 1:3 strategy does not exist in this data** (and almost certainly not anywhere
robustly — it would be a +1R/trade money machine). What's real is the **win-rate ↔ RR
tradeoff**, and you can only pick a point on it:

- **High win rate, small RR:** RSI-2 natural exit — ~74% win, reward:risk < 1:1, PF 2.35.
- **High RR (1:3), low win rate:** ~25–35% win, expectancy near zero to slightly positive
  for the best entry (RSI-2), negative for momentum/breakout.

You get one corner or the other, never both. The closest thing to a real edge *at* 1:3 is
RSI-2's +0.20R, but that's a worse use of the RSI-2 signal than its own natural exit.
Recommendation: don't target the 50%/1:3 unicorn — choose by what you can tolerate
(frequent small wins vs occasional big ones). The strongest validated edges remain RSI-2
(risk-adjusted) and overnight drift (return); see `2026-06-17-strategy-backtest-summary.md`.
