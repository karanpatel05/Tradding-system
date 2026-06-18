# RTH Gating + Cost-Aware Profit Optimization (EMA Crossover / strategy_1)

Date: 2026-06-05
Status: Approved design — ready for implementation plan

## Problem

A 1-year portfolio backtest of strategy_1 (9/15 EMA crossover, 10-stock watchlist,
$2,000, 2:1 RR, both directions) netted **−$557.95 (−27.9%)**, win rate 30.7%,
profit factor 0.92. Analysis showed:

- **~48% of trades enter outside regular trading hours (RTH)** because the crossover
  strategy — unlike the scalper — has **no session/entry-time gate**. Those
  extended-hours trades account for the entire loss (RTH-only entries were +$136
  gross; extended-hours entries −$694).
- The backtest models **no transaction costs**. Even the cleanest gross-positive
  subset (long + RTH, +$216) goes **negative at ~2 bp/side slippage** and is −$661
  at 5 bp. There is no demonstrated cost-survivable edge as currently configured.

## Goal

1. Restrict the crossover to RTH (gate entries + force-flat at the close), applied
   **identically to backtest and live** so backtest/live parity is preserved.
2. Make the optimizer maximize **net-of-cost** profit (slippage modeled) so the
   "max profit" search reflects money actually keepable, not cost-free noise.
3. Run the optimization and report the best config honestly — including the
   possibility that no config clears realistic costs.

## Non-goals

- Do **not** filter extended-hours *bars* out of the data series. EMAs continue to
  compute over all bars; only entries/exits are time-gated. (Filtering bars would
  diverge backtest from live and break parity — established earlier.)
- No change to the EMA/crossover signal math, the HTF filter, or the live execution
  path beyond what the shared decision function already does.
- No train/test split (full-year in-sample chosen by user); overfitting risk is
  accepted and will be noted in the report.

## Design

### Component 1 — RTH session gate (core code change)

Mirror the scalper's proven helpers (`is_session_open`, `is_past_cutoff`,
`_parse_hhmm`, `_et` in `ema_scalper/runner.py`).

New params on `EmaCrossoverParams` (with `xover_session_*` settings + defaults):
- `use_rth_filter: bool = True`
- `session_start_et: str = "09:30"`  — earliest entry
- `session_cutoff_et: str = "15:55"` — latest entry (no new entries at/after)
- `session_close_et: str = "16:00"`  — force-flat any open position at/after

Wiring (single parity point — `signals.py` `_decide` / `replay`, used by both
`live_decision` and `backtest_portfolio`):
- **Entry gate:** emit `enter` only when `use_rth_filter` is False OR the bar's ET
  time ∈ [session_start_et, session_cutoff_et).
- **Force-flat:** when a position is open and the bar's ET time ≥ session_close_et,
  emit `exit` reason `eod` (enum value already exists) at that bar's close.

Because the live worker already acts on `exit` actions (submits the close), this
flows to live unchanged. Entry gate blocks re-entry after the close, so no
extended-hours re-entry.

### Component 2 — Slippage cost model

Add `slippage_per_side_bps: float` (default 5.0, configurable) threaded into the
portfolio summarizer (`_summarize_portfolio` / portfolio row P&L). Each trade's
P&L is reduced by:

    cost = (slippage_per_side_bps / 10_000) * shares * (entry_price + exit_price)

This makes `net_profit`, `expectancy`, `profit_factor`, and `sharpe` net of cost,
so the optimizer's objective reflects keepable profit. Tastytrade equities are
commission-free, so slippage is the only modeled cost.

### Component 3 — Optimization run (no permanent code)

With RTH on + 5 bp slippage, sweep via the existing `optimize_grid`:
- Grid: `risk_reward ∈ {1.5, 2.0, 2.5, 3.0}`, `stop_atr_mult ∈ {1.0, 1.5, 2.0}`,
  `vol_multiplier ∈ {1.2, 1.5, 2.0}` × directions {long, short, both} ≈ 108 combos
  (< MAX_COMBOS 600).
- Objective: `net_profit` (now net of slippage), full-year in-sample.
- Output: ranked top configs + winner's full metrics + an honest assessment.

## Testing (TDD)

1. Entry gate: no `enter` actions emitted for bars outside 09:30–15:55 ET.
2. Force-flat: an open position is exited with reason `eod` at the first bar ≥16:00 ET.
3. Parity: `live_decision` (latest-bar) and `replay` (precomputed) emit identical
   entry/exit decisions over the same bar series with the gate on.
4. Slippage: a known trade's P&L is reduced by exactly the computed cost.
5. `use_rth_filter=False` reproduces pre-change behavior (regression guard).

## Risks / honest caveat

The earlier cost analysis showed the best subset broke even at ~1.2 bp/side. The
optimizer may only surface the least-losing config. The report will state plainly
whether any config is net-profitable at 5 bp, not oversell.

## Notes

- Repo is not under git; spec is written to disk but not committed.
