# Options Strategy Builder — design

**Date:** 2026-07-04
**Status:** Approved (brainstorm) → implementation

## Goal

A new **Builder** tab where the user hand-builds an arbitrary multi-leg options structure,
sees its **payoff diagram** (Black-Scholes, so calendars draw correctly), and **saves it as a
tracked paper trade** with exit rules (target profit % of net debit, stop-loss %). A
background monitor **keeps each saved trade's P&L updated automatically** from live option
mids and **auto-closes** the paper trade when the target or stop is hit.

**No live orders, ever.** Nothing is transmitted to IBKR. `submit_combo_order` is not called.
The only IBKR interaction is *reading* option quotes (mids) to price legs and value trades.

## Constraints established from the code

- **No options price history** from IBKR → no historical backtest. The "performance graph" is
  the **payoff diagram** (build-time) plus **live P&L tracking** of saved trades over time.
- **No implied vol / greeks** in the quote path (`normalize_ticker` returns bid/ask/last/close
  only; option quotes are requested without greek ticks). → BS uses a **manual IV input**
  (default 0.25), not auto-IV. (Future: solve IV per leg from its mid, or request tick 106.)
- **No pricing model** exists. → add a small pure Black-Scholes module.
- `OptionPositionRecord.calendars` is `[{strike,sell,buy}]` — cannot hold arbitrary legs →
  **new table**.
- Live valuation must be **generic** (`Σ sign·qty·mid`), not TC's calendar-specific
  `position_value`.

## Data model

New table `BuilderPositionRecord` (created by `Base.metadata.create_all` — no ALTER needed):

| column | type | notes |
|---|---|---|
| id | int PK | |
| underlying | str | e.g. QQQ |
| legs | JSON | `[{action:"BUY"/"SELL", right:"C"/"P", strike:float, expiry:"YYYY-MM-DD", qty:int}]` |
| net_debit | float | entry cost per 1x, from mids at save time (debit > 0, credit < 0) |
| iv | float | IV used for the saved payoff preview |
| target_pct | float\|null | target profit as fraction of net_debit (e.g. 0.10) |
| stop_pct | float\|null | stop-loss as fraction of net_debit (e.g. 0.25) |
| status | str | `open` \| `closed` |
| entry_time | datetime | |
| exit_time | datetime\|null | set on (paper) close |
| exit_reason | str\|null | `target` \| `stop` \| `manual` |
| pnl | float\|null | realized $ P&L at close |
| last_value | float\|null | latest per-1x value from the monitor |
| last_pnl | float\|null | latest unrealized $ P&L |
| last_pnl_pct | float\|null | latest P&L as fraction of net_debit |
| valued_at | datetime\|null | last monitor tick that valued it |

All dollar figures are **per contract ×100** at display time; stored values are per-1x
(per-share) to match how mids come back, consistent with the TC store.

## Core logic (pure, unit-tested)

`app/options/blackscholes.py`
- `norm_cdf(x)` via `math.erf`.
- `bs_price(S, K, T_years, iv, r, kind) -> float` — European call/put. `T<=0` → intrinsic.

`app/options/combo.py`
- `LEG_SIGN = {"BUY": +1, "SELL": -1}`.
- `combo_value(legs, mid_of) -> float | None` = `Σ sign(action)·qty·mid_of(leg)`; `None` if any
  required mid is missing. This is entry cost at save (with entry mids) and current value at
  monitor time (with live mids). **P&L = value − net_debit.**
- `payoff_at(legs, S, eval_date, iv, r) -> float` — value of the structure at underlying price
  `S` on `eval_date`: each leg → `bs_price(S,K,T,iv,r,kind)` where `T` = years from `eval_date`
  to the leg's expiry (`T<=0` → intrinsic). Returns the structure value (`Σ sign·qty·leg`).
- `payoff_curve(legs, net_debit, eval_date, iv, r, lo, hi, points) -> [{price, pnl}]` where
  `pnl = (payoff_at − net_debit) · 100`. Default price range: ±~20% around the ATM/mean strike;
  default `eval_date` = the **nearest** leg expiry (shows the calendar "tent").

**Exit rule (debit structures):** with `net_debit > 0`, `pnl_pct = (value − net_debit)/net_debit`;
close `target` when `pnl_pct >= target_pct`, `stop` when `pnl_pct <= −stop_pct`. When
`net_debit <= 0` (credit) the %-exit is disabled (trade still tracks unrealized $ P&L).

## Backend API — `app/routers/builder.py` (prefix `/api/builder`)

Reads/quotes require the broker connection; valuation degrades gracefully when disconnected.

- `POST /quote` — body `{underlying, legs}` → fetch each leg's live mid (concurrent
  `aoption_mid`), return `{legs:[{...,mid}], net_debit, missing:[...]}`. Used by "Fetch quotes".
- `POST /payoff` — body `{legs, net_debit, iv, r?, eval_date?, lo?, hi?, points?}` →
  `{curve:[{price,pnl}], breakevens:[...], max_profit, max_loss}`. Pure compute (no IBKR).
- `POST /trades` — body `{underlying, legs, net_debit, iv, target_pct?, stop_pct?}` → persist a
  `BuilderPositionRecord` (`status=open`), return it. **No order transmitted.**
- `GET /trades?status=` — list trades (open first), each with live P&L fields + `stale` flag.
- `GET /trades/{id}` — one trade + its payoff curve (recomputed) for the detail view.
- `POST /trades/{id}/close` — manual paper close at the latest value (`exit_reason=manual`).

## Background monitor — `app/options/monitor.py`

Single app-lifespan task (`asyncio.create_task(run_builder_monitor())` in `main.py`, like
`gw.supervise()`), interval `builder_monitor_interval_seconds` (default 60).

Each tick, **only when the gateway is READY and the regular market is open** (else skip so a
stale/after-hours mid doesn't trigger a false exit — mirrors the TC gate):
1. Load all `status=open` builder trades.
2. For each, fetch its legs' live mids (concurrent), compute `value = combo_value(...)`.
   - `None` (missing mid) → skip this trade this tick.
   - Persist `last_value`, `last_pnl = (value−net_debit)·100`, `last_pnl_pct`, `valued_at`.
   - If a debit trade and `pnl_pct` crosses `target_pct`/`−stop_pct` → **paper-close**:
     `status=closed`, `pnl=last_pnl`, `exit_time=now`, `exit_reason` in {`target`,`stop`}, and
     `log_event(..., category="trade")`.

Durable across restart (state lives in the table; the loop just reloads open rows). `stale` =
`valued_at` older than ~2 intervals (surfaces in the UI instead of a misleading fresh P&L).
No orders, so no kill-switch/flatten coupling is required; the task ends on app shutdown.

## Frontend — new **Builder** tab

`frontend/src/components/BuilderTab.jsx` + `PayoffChart.jsx` (+ small leg-row + trade-list
subviews). Dark theme, indigo accent, Recharts (already used).

1. **Build** — underlying input; a legs table (add/remove rows: BUY/SELL · C/P · strike ·
   expiry · qty). "Fetch quotes" → per-leg mid + **net debit/credit**.
2. **Preview** — IV field (default 0.25), eval-date selector (default nearest expiry), and the
   **payoff chart** (P&L vs. underlying) with breakeven markers and a zero line. Recharts area/line.
3. **Save** — target % and stop % inputs, then **Save trade** (persists; no transmission). A
   short confirmation that this is a *tracked paper trade*, not a live order.
4. **Tracked trades** — list of saved trades (open + closed) with live P&L, target/stop
   progress, and `stale` badge; click for detail + payoff. Open rows poll (~15s) like the TC page.

`frontend/src/api.js`: `builderQuote`, `builderPayoff`, `builderSaveTrade`, `builderTrades`,
`builderTrade`, `builderCloseTrade`. `App.jsx`: add `Builder` to `TABS` + sidebar icon.

## Testing

- **Black-Scholes** (`test_options_bs.py`): known-value checks (ATM call/put put-call parity;
  `T=0` → intrinsic; deep ITM/OTM limits).
- **combo/payoff** (`test_options_combo.py`): `combo_value` sign/qty math; `payoff_at` for a
  vertical (clean intrinsic at expiry) and a calendar (long leg retains BS value at the near
  expiry → tent shape); breakeven/max-profit extraction.
- **Endpoints/monitor** (`test_builder_trades.py`): save → list returns it; `/payoff` returns a
  curve; monitor tick with a fake mid source drives a debit trade to `target` and to `stop`
  (auto-close, correct `exit_reason`/`pnl`); credit trade tracks P&L but never %-auto-closes;
  restart re-loads open rows (no id-recovery bug — state is in the table).
- **Manual**: run the app, build a calendar in the Builder tab, confirm the payoff tent renders,
  save it, confirm it appears in the tracked list with a P&L that updates.

## Non-goals (v1)

- No live/paper **order transmission** (save-and-track only).
- No auto-IV (manual IV; per-leg IV-solve is a future enhancement).
- No adjustments/rolling (target/stop paper-close only).
- No per-leg quantity in any *order* (moot — no orders); qty is used only in valuation/payoff.
- Credit structures: buildable/previewable/savable, but no %-based auto-exit.
