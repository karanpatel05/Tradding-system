# Contracts (quantity) selector — Triple Calendar + Builder — design

**Date:** 2026-07-05
**Status:** Approved (brainstorm) → implementation

## Goal

Let the user choose how many **contracts** (whole multiples of the structure) to trade:
- **Triple Calendar** — a "Contracts" setting in Controls; the live loop opens that many of the
  three-calendar spread per entry, and P&L scales accordingly.
- **Builder** — a "Contracts" multiplier when saving a tracked paper trade; the monitor scales
  the tracked P&L accordingly.

Today both are hardwired to 1 contract (the combo order uses quantity 1; P&L is `×100` for a
single contract).

## Shared model

`contracts` is an **integer ≥ 1** whole-structure multiplier. Net debit stays **per-1x** (per
spread). It scales two things:
- **Order quantity** (TC only — the combo BAG order quantity).
- **Dollar P&L:** `pnl$ = (value − net_debit) × 100 × contracts`. Percent target/stop and
  progress meters are unaffected (they're ratios of net_debit).

## Part A — Triple Calendar

### DB (additive migrations in `db/base.py::_ensure_added_columns`)
- `StrategyState.contracts: int = 1` — persisted, runtime-settable (like `capital_allocation`).
  `ALTER TABLE strategy_state ADD COLUMN contracts INTEGER DEFAULT 1`.
- `OptionPositionRecord.contracts: int = 1` — what the position was opened with (so P&L and
  closing orders use the right size even if the setting later changes).
  `ALTER TABLE option_positions ADD COLUMN contracts INTEGER DEFAULT 1`.

### Endpoint (`routers/strategies.py`)
`PUT /api/strategies/{key}/contracts` body `{contracts: int}` → clamp to `>= 1`, set
`StrategyState.contracts`, log it. Mirrors the existing `/allocation` route (auth-gated).
The `/{key}` detail response gains `contracts` (from StrategyState).

### Execution + live loop
- `execution.submit_combo_order(..., quantity: int = 1)` — use `quantity` in the order:
  `LimitOrder(action, quantity, net_price)` / `MarketOrder(action, quantity)` (was hardcoded 1).
- `legs.Position` gains `contracts: int = 1` (the unused `size` field stays or is replaced —
  implementer's call; don't break `decide_entry`'s `pos.size` line — leave `size` as is and add
  `contracts` separately).
- `live.manage_step(..., contracts: int = 1)`: on **entry** and **roll re-entry**, set
  `pos.contracts = contracts` and place the order with `quantity=contracts`; on **adjust/exit**,
  place with `quantity=position.contracts`. (All orders for one position use that position's
  contracts.)
- `strategy_class.run_live`: read `StrategyState.contracts` each tick and pass it to
  `manage_step(..., contracts=...)`, so a runtime change takes effect on the next entry. The
  executor (`_make_executor`) forwards `quantity` to `submit_combo_order`.
- `store.save_position(..., contracts)` persists `OptionPositionRecord.contracts`;
  `load_open` restores it onto the `Position`.

### P&L (options-position endpoint)
Multiply the dollar figures by the position's `contracts`:
`unrealized_pnl = (value − net_debit) × 100 × contracts`. Return `contracts` in the payload.
`net_debit`, `value`, target/stop values, and percentages stay per-1x.

### Semantics
Changing Contracts applies to the **next entry**. An open position keeps the contracts it was
opened with (stored on its record); rolls re-enter at the current setting.

### Frontend (`OptionsStrategyDetail.jsx`)
Controls card: a **Contracts** number input (min 1, default from `detail.contracts`) + a "Set"
button calling `api.setContracts(keyName, n)` (`PUT /contracts`). Show "Contracts: N". The
Open Position P&L already arrives scaled. `api.js`: `setContracts(key, contracts)`.

## Part B — Builder

### DB
- `BuilderPositionRecord.contracts: int = 1` — additive migration (the table already exists):
  `ALTER TABLE builder_positions ADD COLUMN contracts INTEGER DEFAULT 1` in `_ensure_added_columns`.

### Save + store
- `POST /api/builder/trades` `SaveBody` gains `contracts: int = 1` (Pydantic `ge=1`);
  `store.save_trade(..., contracts)` persists it.

### Monitor P&L (`options/monitor.py`)
`last_pnl = round((value − net_debit) × 100 × contracts, 2)` (read `contracts` off the record).
`combo_value` already applies per-leg qty; `contracts` multiplies the whole structure on top.
`last_value`/`last_pnl_pct` stay per-1x (pct is ratio, unaffected).

### Frontend (`BuilderTab.jsx`)
Save panel: a **Contracts** number input (min 1, default 1) next to Target %/Stop %;
`builderSaveTrade` body includes `contracts`. Tracked-trades P&L already arrives scaled.

## Testing

- **TC:** contracts endpoint clamps `<1` to 1 and sets StrategyState; `submit_combo_order`
  passes `quantity` into the order (assert the order's `totalQuantity`); options-position P&L
  is `×contracts` (endpoint test with a seeded record carrying `contracts=2`); `manage_step`
  entry stamps `pos.contracts` and orders with that quantity (fake executor spy records qty).
- **Builder:** `save_trade` stores `contracts`; the monitor scales `last_pnl ×contracts`
  (extend `test_builder_monitor` with a `contracts=3` trade → pnl ×3).
- **Migrations:** `init_db()` twice is idempotent; the 3 new columns exist and default to 1.

## Non-goals

- No allocation-based auto-sizing (contracts is explicit; `capital_allocation` stays
  informational for TC).
- No per-leg ratio changes for TC (still ratio-1 legs × contracts).
- No fractional contracts.
