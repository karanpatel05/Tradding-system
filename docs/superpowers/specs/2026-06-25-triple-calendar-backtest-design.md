# Triple Calendar Strategy — Phase 1: Position Model + Backtest — Design Spec

Date: 2026-06-25
Status: Approved — implement.

## Goal
Build the Triple Calendar options strategy's **core logic + a historical backtest**, on real
Polygon option data, with **modeled IV** (Black-Scholes inversion) so we can report an
IV-regime breakdown. The core (position model, strike/expiry selection, entry/exit/adjustment
rules) is written so **live execution (Phase 2) reuses it unchanged**. Live order placement,
IBKR combo execution, and registry/dashboard wiring are **out of scope for Phase 1**.

## Decisions (locked)
- IV approach **B**: price-based backtest on Polygon option aggregates **plus** a BS
  inversion for approximate IV / regime breakdown.
- Build order: position model + backtest first (this spec); live = a later spec.
- Backtest window: the current Polygon key (~2yr).
- New strategy-specific files only in Phase 1; no changes to shared/core modules
  (`execution.py`, registry, dashboard backtest engine) yet.

## Strategy recap (rules implemented)
Delta-neutral, positive-vega: three calendars (lower / ATM / upper). Each calendar = sell a
21-DTE Friday option, buy the 28-DTE Friday (same strike). Entry: ATM = underlying rounded to
$5; expected move = 21-DTE ATM straddle (call mid + put mid); outer strikes = ATM ±
(expected_move + buffer), rounded to $5; optional manual ±5 nudge. Filters: block entry if
the sell-leg expiry falls after an injected macro-event date; if VIX < threshold, scale size
down (don't skip). One open trade per underlying; re-entry allowed. Exit: profit target =
10% of net debit; time stop = sell-leg 7 DTE; breach adjustment = add one further-out calendar
in the breached direction (same cycle), up to max calendars (4, config 5th); never close
breached calendars early.

## Modeling assumptions (honest — this is where fidelity lives)
- **Option price proxy:** Polygon daily aggregates give OHLCV per contract, not bid/ask. We
  use the **daily close** as each leg's mark (and as the "mid" for the entry straddle). No
  intraday fills; entries/exits resolve on daily closes.
- **IV:** computed per leg by inverting Black-Scholes from the daily close, given underlying
  close, strike, time-to-expiry, `tc_risk_free_rate` (config), dividend yield `tc_div_yield`
  (config, default 0). European BS used for all (index options are European; ETF options are
  American — accepted approximation, documented).
- **VIX:** optional. If a VIX daily series is available (Polygon index `I:VIX` when entitled,
  or an injected series), the low-vol size-scaling + regime buckets use it; otherwise the
  filter is a no-op and the regime breakdown falls back to entry ATM-IV buckets.
- These approximations make P&L %, win rate, hold days, and drawdown faithful to real option
  prices; IV figures are modeled estimates, not vendor greeks.

## Architecture — new package `app/strategies/triple_calendar/`

- **`params.py`** — `TripleCalendarParams` dataclass: `underlying="QQQ"`, `sell_dte=21`,
  `buy_dte=28`, `strike_round=5.0`, `em_buffer=5.0`, `manual_nudge=0`, `profit_target=0.10`,
  `time_stop_dte=7`, `vix_threshold=13.0`, `vix_scale=0.5`, `max_calendars=4`,
  `allow_fifth=False`, `risk_free_rate=0.04`, `div_yield=0.0`, `event_dates: list[date]`.
- **`iv.py`** (pure) — `bs_price(kind, S, K, T, r, sigma, q) -> float` and
  `implied_vol(price, kind, S, K, T, r, q) -> float|None` (bisection; None if no solution).
- **`legs.py`** (pure) — data model + selection:
  - `OptionLeg(symbol, expiry, strike, right, action)` , `Calendar(strike, sell_expiry,
    buy_expiry)`, `Position(calendars, entry_date, net_debit)`.
  - `nearest_friday(target: date)`, `target_expiry(entry, dte)` (≈dte days out → nearest
    Friday), `atm_strike(price, round_to)`, `outer_strikes(atm, expected_move, buffer,
    round_to, nudge)`, `expected_move(call_mid, put_mid)`.
  - Adjustment state machine: `breached(position, price)` → which side; `add_calendar(...)`
    respecting `max_calendars`/`allow_fifth`; never removes existing calendars.
- **`pricing.py`** — `OptionPriceSource` protocol: `leg_close(ticker, day) -> float|None`,
  `chain_strikes(underlying, expiry) -> list[float]`, `underlying_close(symbol, day)`. A
  `PolygonPriceSource` implementation backed by the existing `app/options` provider (option
  aggregates) + Polygon stock aggregates for the underlying. Caches per-contract daily series
  in-memory per run (pacing-friendly). The protocol lets Phase-2 live swap a live source.
- **`strategy.py`** — the **shared core**: `decide_entry(day, source, params, events, vix)`,
  `manage(position, day, source, params)` → exit/adjust decisions. Pure given a price source;
  reused by backtest now and live later.
- **`backtest.py`** — run loop over the date range: find entries, build positions, mark daily,
  apply profit target / time stop / breach adjustments, record trades. `compute_metrics` →
  P&L % of debit, win rate, avg hold days, max drawdown, total trades, + IV-regime breakdown
  (bucket trades by entry ATM-IV or VIX).
- **`scripts/run_triple_calendar.py`** — runner: pulls data via `PolygonPriceSource`, runs the
  backtest for the configured underlying, prints metrics + regime breakdown (mirrors the
  RSI-2 / overnight research runners).

## Logging
Backtest uses prints in the runner (consistent with the other research runners). Live (Phase
2) will use `log_event(...)` like the equity strategies.

## Testing
- **`iv.py`:** BS price sanity (ATM call/put ordering), and **round-trip** `price→implied_vol→
  bs_price ≈ price` within tolerance; `None` on impossible prices.
- **`legs.py`:** `nearest_friday` (incl. entering on a Friday/weekend), `atm_strike` rounding,
  `expected_move`, `outer_strikes` (buffer + nudge + $5 rounding), breach detection, and
  `add_calendar` cap (4, and 5th only when `allow_fifth`).
- **`strategy.py`:** entry blocked by event-date filter; VIX< threshold scales size; exit at
  10% debit; time stop at 7 DTE; one-trade-per-underlying.
- **`backtest.py`:** a synthetic `OptionPriceSource` stub (deterministic prices) drives a full
  entry→target-exit trade and an entry→time-stop trade; metrics computed correctly.
- **Live data smoke:** `run_triple_calendar.py` against the Polygon key prints a real QQQ
  backtest (or a clean "insufficient option history" message if the 2yr window is too thin for
  a given expiry).

## Out of scope (Phase 2, separate spec)
Live IBKR combo (`Bag`) execution, live chain/expected-move, position tracking + live
adjustments, `execution.py`/registry/config/dashboard wiring, kill-switch options handling.

## Notes
Repo under git. Spec on disk. Phase-1 introduces only new `triple_calendar/` files + tests +
one runner — no shared-module edits.
