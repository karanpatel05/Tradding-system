# Triple Calendar — roll on exit (target & stop) — design

**Date:** 2026-07-04
**Status:** Approved (brainstorm) → implementation

## Goal

Turn the Triple Calendar's two exits into **rolls**: when an open position hits the **+10%
profit target** or the **−25% stop-loss**, close it (realizing the P&L) and **immediately
re-enter a fresh three-calendar structure centered at the current underlying level**, in the
same tick — provided entry conditions allow. The strategy becomes "always re-center": it
harvests the +10% / −25% cycle continuously and recenters on every exit, staying in a
position whenever conditions permit.

The strike-breach adjustment (adds one calendar further out) is **unchanged**.

## Key risk context (explicit)

- Rolling on the −25% stop **removes the loss cap** and can compound losses in a trend
  (repeated −25% rolls). **No roll cap** was chosen; the **VIX ≤ 16 entry gate is the only
  backstop** (a violent move usually pushes VIX > 16, which pauses re-entry).
- **There is no backtest** for the Triple Calendar (IBKR has no options price history), so
  this behavior can only be validated on paper/live. The `roll_on_exit` toggle (below) is the
  kill switch.

## Design

### Parameters (`params.py`)

- `roll_on_exit: bool = True` — when true, +10% target and −25% stop **roll** (close + reopen);
  when false, they behave as today (plain exit; the loop re-enters on the next scan).
- `vix_wait_poll_seconds: int = 5` — while flat and blocked by the VIX gate, re-check VIX at
  this cadence (instead of the 300s manage interval) so entry fires promptly once VIX ≤ 16.

### Detection — `manage()` (pure)

When `roll_on_exit` is true, the target and stop return a new action **`"roll"`** instead of
`"exit"`:

```
if pnl_pct >= profit_target:      return "roll", {"reason": "target",    "value": v, "pnl_pct": p}
if pnl_pct <= -stop_loss_pct:     return "roll", {"reason": "stop_loss", "value": v, "pnl_pct": p}
```

When `roll_on_exit` is false, these return `"exit"` exactly as today (preserving current
behavior and the classic `test_tc_exit`). Breach → `"adjust"`, otherwise `"hold"` — unchanged.

`manage()` is pure and has no re-entry ability; it only re-labels the exit as a roll. The
close+reopen happens in `manage_step`.

### Execution — `manage_step()`

New `"roll"` branch (reason-agnostic — target and stop roll identically):

1. Determine `value` (from `info["value"]` or recompute); `closed_pnl = value − net_debit`.
2. **Close:** `executor(underlying, exit_legs(position), net_price=None, action="SELL",
   reason="roll_"+reason)`.
3. **Reopen at current level:** `new_pos = decide_entry(day, source, params, events, vix)`.
   `decide_entry` already re-centers on the current spot's ATM and enforces the entry gates
   (**VIX ≤ 16**, no event during the sell leg, positive debit).
   - **Fail-closed on VIX:** if `vix is None`, do **not** reopen (treat as `new_pos = None`),
     mirroring the entry fail-closed rule — never reopen without a VIX reading.
   - If `new_pos` is not None: `executor(..., entry_legs(new_pos), net_price=round(net_debit,2),
     action="BUY", reason="roll_entry")`; return
     `{"action": "roll", "position": new_pos, "info": {"reason": reason, "closed_pnl": closed_pnl, "reopened": True}}`.
   - If `new_pos` is None (VIX > 16 / unreadable / event / no debit): return
     `{"action": "roll", "position": None, "info": {"reason": reason, "closed_pnl": closed_pnl, "reopened": False}}`
     — closed and flat; the loop re-enters later when conditions allow.

Because `decide_entry` needs `vix`, **`manage_step` must receive `vix` on every tick** (see
loop change), not only when flat.

### Persistence / journal — `_persist()`

New `"roll"` branch (when `record_id is not None`):

```
await close_position(record_id, pnl=info["closed_pnl"])
log "ROLL close (<reason>) pnl <closed_pnl>"  (category="trade")
new_pos = out["position"]
if new_pos is not None:
    rid = await save_position(self.key, new_pos, order_ids=[], paper=not enable_live_orders)
    log "ROLL reopen <underlying> debit <net_debit>"  (category="trade")
    return new_pos, rid
return None, None
```

Note: the tick's `snapshot` was resolved for the *old* position, so the just-opened new
position's legs won't be valued this tick — the existing value-persistence block computes
`position_value(snapshot, new_pos)` → `None` → skips, and values it on the next tick. Fine.

### Live loop — `run_live()`

1. **Fetch VIX every tick** (currently only when flat). Keep the flat fail-closed skip:
   when `position is None and vix is None` → skip the entry attempt this tick. When holding,
   `vix` is still fetched so a roll can gate its re-entry.
2. **Pass `vix` into `manage_step` on every tick.**
3. **Adaptive sleep** at the end of the loop body:
   - `poll = tc_manage_interval_seconds` (default 300s).
   - If `position is None` **and** (`vix is None` or `vix > vix_entry_max`) → `poll =
     vix_wait_poll_seconds` (5s) — fast-poll while waiting for VIX to fall to ≤ 16.
   - The existing not-READY / market-closed guard keeps its 300s pause (no fast-poll when we
     can't trade).
   Timing honesty: `avix()` itself settles ~2s, so a "5s" wait is a ~5–7s effective cadence.

### Metadata (`strategy_class`)

Update `exit_logic`/`rules` to state that +10% and −25% **roll into a fresh position at the
current level (VIX ≤ 16)** rather than closing flat.

## Testing

No backtest possible (no options history). Unit tests (pure + `manage_step` with a fake
source/executor, following `test_tc_exit`/`test_tc_manage_step` patterns):

- `manage()` with `roll_on_exit=True`: +10% → `("roll", reason="target")`; −25% →
  `("roll", reason="stop_loss")`. With `roll_on_exit=False`: both → `("exit", …)` (classic).
- `manage_step` roll (target and stop): places a SELL close then a BUY re-entry; returns
  `action="roll"`, `position` = a new position centered at the current ATM, `reopened=True`,
  `closed_pnl` sign correct (+ for target, − for stop).
- `manage_step` roll blocked: `vix=None` (fail-closed) and `vix > 16` each close **without**
  reopening (`position=None`, `reopened=False`, exactly one SELL, no BUY).
- Loop cadence (live-style test à la `test_tc_ready_gate`): flat + VIX > 16 → loop sleeps the
  5s poll and re-checks; entry fires once VIX ≤ 16. (Fetch VIX every tick while holding, too.)
- Update `test_tc_exit` to run with `roll_on_exit=False` for the classic exit assertions.

## Non-goals

- No roll cap / daily loss limit (VIX gate is the backstop, per decision).
- No change to the +10%/−25% thresholds, the strike-breach adjustment, or sizing.
- No change to how VIX is sourced (the `Index('VIX','CBOE')` / `avix()` added previously).
