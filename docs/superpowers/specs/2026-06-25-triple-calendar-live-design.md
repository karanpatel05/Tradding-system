# Triple Calendar — Phase 2: Live Trading — Design Spec

Date: 2026-06-25
Status: Approved — implement (shared-module changes pre-approved, see list).

## Goal
Trade the Triple Calendar live on IBKR, **reusing the Phase-1 core (`decide_entry`/`manage`)
unchanged** by swapping the price source from Polygon-historical to an IBKR-live one. Full app
integration (activatable strategy slot, dashboard, kill-switch, persistence). **Paper-safe by
default** — nothing transmits without `ENABLE_LIVE_ORDERS=1`.

## Decisions (locked)
- **Single 6-leg combo** entry (one IBKR BAG = the 3 calendars). Each **breach adjustment** =
  one additional 2-leg calendar BAG. Exit = closing combo(s) for all open legs.
- **IBKR live option quotes** for entry (reqMktData mids). *Risk:* needs the account's OPRA
  real-time entitlement + market open (unconfirmed). Built against a quote interface so a mock
  validates everything but real fills; falls back cleanly when quotes are empty.
- **Full app integration.**

## Architecture

### New (strategy-specific) — `app/strategies/triple_calendar/`
- **`live_source.py`** — `LivePriceSource` implementing the Phase-1 interface
  (`underlying_close(sym, day)`, `chain_strikes(underlying, expiry)`,
  `option_close(underlying, expiry, strike, right, day)`), backed by IBKR: underlying last via
  `ibkr.market.get_quotes`; option mid via `reqMktData` on a qualified `Option`; chain via
  `reqSecDefOptParams`. (`day` is "today"; live source ignores historical dates.)
- **`strategy_class.py`** — `TripleCalendarStrategy(BaseStrategy)`: `ready=True`, `is_options=
  True`, overview/rules metadata, and `run_live()` — the manage loop (enter when flat → daily
  manage: target/time-stop/breach-adjust → exit). `scan()` returns `[]` (not a stock-picker).
- **`live.py`** — live manager helpers: build the 6-leg combo legs from a `Position`, place
  entry/adjustment/exit via `execution.submit_combo_order`, persist via `OptionPositionRecord`,
  and `log_event` on enter/adjust/exit. Uses Phase-1 `decide_entry`/`manage`.
- Tests: mock `LivePriceSource` + stubbed `submit_combo_order` drive enter→adjust→exit;
  combo-leg construction; persistence round-trip.

### Shared/core changes (pre-approved; all additive, equity paths untouched)
1. **`app/ibkr/contracts.py`** — `option(underlying, expiry: date, strike, right) -> Option`
   and `combo(underlying, legs) -> Contract` (BAG with `ComboLeg(conId, ratio, action)`).
2. **`app/engine/execution.py`** — `submit_combo_order(underlying, legs, *, net_price=None,
   reason="") -> dict`: qualify each option leg (conId), build the BAG, place a net-debit
   `LimitOrder` (or market if `net_price` None). **Gated by `ENABLE_LIVE_ORDERS`** (off → log
   the intended legs, return `transmitted: False`). Extend `flatten_all` to also close option
   positions (BAG/legs), so the kill switch covers them.
3. **`app/db/models.py`** — new `OptionPositionRecord` (id, strategy_key, underlying, status
   open|closed, entry_time, net_debit, `legs: JSON`, `calendars: JSON`, combo order ids,
   exit_time, pnl). `create_all` makes it on boot.
4. **`app/engine/worker.py`** — dispatch branch: `if getattr(strategy, "is_options", False):
   await strategy.run_live()` (added before the equity branches).
5. **`app/config.py`** — `tc_underlying="QQQ"`, `tc_event_dates=""` (comma ISO dates),
   `tc_manage_interval_seconds=300`, `tc_enabled` (slot ready flag if desired).
6. **`app/strategies/registry.py`** — register `TripleCalendarStrategy()` into a slot (e.g.
   `strategy_2`) so it appears + activates in the dashboard and is covered by the kill switch.

### Order construction (IBKR / ib_async)
- Per option leg: `Option(symbol, expiry "YYYYMMDD", strike, right, "SMART")`,
  `qualifyContractsAsync` → `conId`.
- Entry BAG: 6 `ComboLeg`s — per calendar: sell-near `action="SELL"`, buy-far `action="BUY"`,
  ratio 1. Combo order `BUY` at net-debit limit (mid of the spread) ⇒ pay debit.
- Adjustment: 2-leg BAG (sell-near + buy-far at new strike), same pattern.
- Exit: a BAG that reverses all currently-open legs (`SELL` the spread), or market-close legs.

### Live flow (`run_live`)
1. On start: reconstruct any open `OptionPositionRecord` for this strategy (restart-safe).
2. Loop every `tc_manage_interval_seconds`:
   - If flat: `pos = decide_entry(today, live_source, params, events, vix)`. If not None →
     `submit_combo_order(entry legs)`, persist record, log ENTER.
   - If open: `action, info = manage(pos, today, live_source, params)`. On `adjust` →
     `submit_combo_order(new calendar)`, update record, log ADJUST. On `exit` →
     `submit_combo_order(closing legs)`, mark record closed + pnl, log EXIT.
3. Status surfaced via `StrategyState` like other strategies.

## Safety
- `ENABLE_LIVE_ORDERS` is the single gate. Off (default) → `submit_combo_order` logs the exact
  legs and returns `transmitted: False`; `run_live` still persists/marks a **paper** position
  so the dashboard/journal reflect it, but no broker order is sent.

## Testing
- **Unit:** combo-leg construction from a `Position` (6 legs, correct actions/strikes/expiries);
  `submit_combo_order` paper-mode gating (no transmit); `OptionPositionRecord` persistence
  round-trip; `run_live` enter→adjust→exit against a mock quote source + stub execution.
- **Live smoke (user, needs OPRA + market open):** `scripts/triple_calendar_live_smoke.py` —
  connect, build the QQQ entry combo, log it in paper mode; with `ENABLE_LIVE_ORDERS=1` place
  + then flatten a 1-lot paper position.

## Out of scope
Historical P&L charting, multiple concurrent underlyings, automatic event-date fetching
(events stay an injected list), option-aware position UI beyond the generic strategy detail.

## Notes
Repo under git. Spec on disk. If OPRA quotes are unavailable, live entry degrades to "no
quote → no entry" (logged); the mock-validated plumbing is still correct.
