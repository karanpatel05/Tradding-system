# Alpaca Order Routing (paper-locked, shared account) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send this app's orders to Alpaca paper (only ever paper, only ever touching the app's own tagged orders/positions), keep Tastytrade for market data only, and remove the Triple Calendar strategy.

**Architecture:** A small Alpaca HTTP client (`app/broker/alpaca_client.py`) owns the base URL, auth headers, the paper lock and the `tsys-` ownership tag. A ledger module derives the app's own net positions from Alpaca's record of the app's tagged orders. `app/broker/account.py` and `app/broker/orders.py` keep their public names/shapes but become Alpaca-backed and ownership-scoped, so `worker.py`, the kill switch, reconciliation, dashboard and WebSocket callers don't change shape.

**Tech Stack:** Python 3.14, FastAPI, async SQLAlchemy (SQLite), httpx (`MockTransport` in tests), pytest + pytest-asyncio; React + Vite frontend.

**Spec:** `docs/superpowers/specs/2026-11-17-alpaca-order-routing-design.md`

## Global Constraints

- Paper host, exact: `paper-api.alpaca.markets`. Every Alpaca **write** (submit, cancel, close) to any other host returns `{"transmitted": False, "detail": "blocked: Alpaca live endpoint locked (paper only)"}` and logs an error.
- Ownership tag: `client_order_id = "tsys-" + <order-store ref>`, max 48 chars. Only `tsys-` orders are ever cancelled, closed, counted or shown as positions.
- Never call `DELETE /v2/positions` or unfiltered `DELETE /v2/orders`.
- No Tastytrade order/cancel/account-position calls remain (Tastytrade = data + auth probe only).
- `ENABLE_LIVE_ORDERS` OFF = internal simulated fills exactly as today (no network order call); ON = Alpaca paper.
- Alpaca mleg ≤ 4 legs; options `time_in_force: "day"`; Alpaca option symbols are unpadded OCC (`QQQ261117C00660000`).
- No network in tests: all Alpaca HTTP via `httpx.MockTransport` (pattern: `backend/tests/test_alpaca_status.py::_patch_transport`).
- Keys and full account numbers never reach responses or logs (mask as `••••` + last 4).
- Run tests from `backend/` with `DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m pytest -q <paths>`; delete `_t.db` after.

## Review Focus

1. **Manual shares in the same symbol as an app position** — flatten/close must send only the app-owned quantity and leave the manual shares (Task 5 test `test_flatten_closes_only_app_qty_when_manual_shares_share_symbol`).
2. **Alpaca unreachable during startup recovery** — must not look like "broker flat"; trip safe mode with a clear "broker unavailable" reason rather than a false clean/false mismatch (Task 6 test `test_recovery_trips_with_broker_unavailable_reason`).
3. **`APCA_API_BASE_URL` with a trailing slash or `/v2` path** (`https://paper-api.alpaca.markets/v2/`) — still paper, must not double the `/v2` or be locked (Task 2 test `test_trading_base_normalizes_paper_url_variants`).
4. **Partially filled app order** — ledger counts `filled_qty`, not `qty` (Task 3 test `test_ledger_uses_filled_qty_for_partial_fills`).
5. **Kill switch with gate OFF while app-owned Alpaca positions remain from an earlier ON period** — transmits nothing, raises listing those positions so the UI reports not-flat (Task 5 test `test_flatten_gate_off_with_app_positions_raises_and_sends_nothing`).

---

### Task 1: Remove Triple Calendar

**Files:**
- Modify: `backend/app/strategies/registry.py` (drop TC import/registration; update header comment)
- Modify: `backend/app/strategies/registry.py::seed_strategy_state` (reset non-ready slot status)
- Create: `backend/app/options/live_source.py` (moved `LivePriceSource` from `app/strategies/triple_calendar/live_source.py`, unchanged)
- Modify: `backend/app/options/chain.py:10` (import from `app.options.live_source`)
- Modify: `backend/app/routers/strategies.py` (remove `_open_option_record`, `_serialize_option_position`, `/options-position`, `/options-position/payoff`, `/contracts`, `/tickers` GET+PUT, `tc_tickers` field in `detail`)
- Modify: `backend/app/config.py:89-91` (remove `tc_underlying`, `tc_event_dates`, `tc_manage_interval_seconds`)
- Modify: `backend/app/db/base.py` (add idempotent one-time data fix, see Step 5)
- Delete: `backend/app/strategies/triple_calendar/`, `backend/tests/test_tc_*.py` (24 files), plus any other test importing `triple_calendar` (find with `grep -rl triple_calendar backend/tests`)
- Frontend: delete `frontend/src/components/OptionsStrategyDetail.jsx`; remove its import/branch in `StrategyDetail.jsx`; remove TC calls in `api.js` (contracts, tickers, options-position, payoff); remove the `options strategy detail (Triple Calendar)` block in `styles.css`; drop TC rows in `frontend/src/redesign/main.jsx`
- Test: `backend/tests/test_tc_removal.py`

**Interfaces:**
- Produces: `app.options.live_source.LivePriceSource` (same class/behaviour as before).
- Produces: `async def close_stale_tc_positions(conn_or_session) -> int` in `app/db/base.py`, called once from `init_db()` after migrations.

- [ ] **Step 1: Write failing tests** in `backend/tests/test_tc_removal.py`:
  - `test_registry_has_no_triple_calendar`: `get_strategy("strategy_2")` is a `ComingSoonStrategy`, `.ready is False`, name `"Strategy 2"`; no registered strategy has class name `TripleCalendarStrategy`.
  - `test_seed_resets_non_ready_slot_status`: insert `StrategyState(key="strategy_2", name="Triple Calendar", ready=True, status="scanning")`, run `seed_strategy_state()`, assert row has `ready False`, `name "Strategy 2"`, `status "idle"`.
  - `test_close_stale_tc_positions_idempotent`: insert two `OptionPositionRecord` rows `strategy_key="strategy_2"`, status `"open"`, plus one open row with another strategy key; run fix → returns 2, both `strategy_2` rows `status == "closed"`, `pnl is None`, closed timestamp set; other row untouched; second run returns 0.
  - `test_builder_chain_imports_live_source`: `from app.options.chain import *` succeeds and `app.options.chain.LivePriceSource is app.options.live_source.LivePriceSource`.
- [ ] **Step 2: Run** `pytest tests/test_tc_removal.py -q` → FAIL.
- [ ] **Step 3: Implement** the registry change, the seed status reset (when `state.ready` becomes False and `state.status != "idle"`, set `"idle"`), the `LivePriceSource` move, route/config removals, and `close_stale_tc_positions` (match the column names on `OptionPositionRecord` in `app/db/models.py`; only rows `strategy_key == "strategy_2" AND status == "open"`).
- [ ] **Step 4: Delete** the TC package, TC tests, and frontend pieces listed above; `grep -rn -i "triple.calendar\|triple_calendar\|tc_tickers\|tcTickers" backend/app frontend/src` → only the kept DB column/model + `db/base.py` migration lines and the Journal's historical label in `CalendarTab.jsx`/`journal.py`.
- [ ] **Step 5: Run** full suite `pytest -q` → all pass; `cd frontend && npm run build` → `✓ built`.
- [ ] **Step 6: Commit** `refactor: remove Triple Calendar strategy (keep history tables, close stale rows)`.

---

### Task 2: Alpaca client + paper lock + ownership tag

**Files:**
- Create: `backend/app/broker/alpaca_client.py`
- Modify: `backend/app/services/alpaca_status.py` (use the client for base URL/headers; behaviour unchanged)
- Test: `backend/tests/test_alpaca_client.py`

**Interfaces:**
- Produces:
  - `PAPER_HOST = "paper-api.alpaca.markets"`, `TAG_PREFIX = "tsys-"`
  - `class PaperLockError(Exception)`; `class AlpacaError(Exception)` with `.status: int`, `.message: str`
  - `def base_url() -> str` — configured URL normalized to scheme+host (strip trailing `/` and a trailing `/v2`).
  - `def trading_base() -> str` — `base_url()` if host == `PAPER_HOST`, else raise `PaperLockError`.
  - `async def request(method: str, path: str, *, params: dict | None = None, json: dict | None = None, write: bool = False) -> Any` — `path` starts with `/v2/`; `write=True` uses `trading_base()`; returns parsed JSON (or `None` for 204); non-2xx raises `AlpacaError` with Alpaca's `message`; 8s timeout; headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`.
  - `def tag(ref: str) -> str` — `TAG_PREFIX + ref`, truncated to 48 chars.
  - `def is_app_owned(client_order_id: str | None) -> bool`
  - `def occ_unpadded(symbol: str) -> str` — removes spaces (`"QQQ   261117C00660000"` → `"QQQ261117C00660000"`); equities unchanged.
  - `def configured() -> bool` — both keys non-empty.

- [ ] **Step 1: Write failing tests:**
  - `test_trading_base_normalizes_paper_url_variants`: for `https://paper-api.alpaca.markets`, `…/`, `…/v2`, `…/v2/` → `trading_base() == "https://paper-api.alpaca.markets"`.
  - `test_trading_base_locks_live_host`: `https://api.alpaca.markets` → `PaperLockError`; `base_url()` still returns it (reads allowed).
  - `test_request_write_to_live_host_never_sends`: live host + `request("POST", "/v2/orders", json={}, write=True)` raises `PaperLockError` and the MockTransport handler is never called.
  - `test_request_raises_alpaca_error_with_message`: 422 `{"message": "insufficient qty"}` → `AlpacaError(status=422, message="insufficient qty")`.
  - `test_tag_and_ownership`: `tag("x"*60)` has len 48 and starts `tsys-`; `is_app_owned("tsys-a")` True; `is_app_owned("abc")`/`None` False.
  - `test_occ_unpadded`: as above; `"AAPL"` → `"AAPL"`.
- [ ] **Step 2: Run** `pytest tests/test_alpaca_client.py -q` → FAIL.
- [ ] **Step 3: Implement** `app/broker/alpaca_client.py`; refactor `alpaca_status.py` onto `base_url()`/headers.
- [ ] **Step 4: Run** `pytest tests/test_alpaca_client.py tests/test_alpaca_status.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(broker): Alpaca HTTP client with paper lock and tsys- ownership tag`.

---

### Task 3: App-owned ledger + Alpaca-backed account views

**Files:**
- Create: `backend/app/broker/ledger.py`
- Modify (rewrite internals, keep names/shapes): `backend/app/broker/account.py`
- Delete/replace: `backend/tests/test_broker_account.py` → `backend/tests/test_broker_account_alpaca.py`
- Test: `backend/tests/test_ledger.py`

**Interfaces:**
- Consumes: Task 2 `request`, `is_app_owned`, `occ_unpadded`.
- Produces (`ledger.py`):
  - `async def app_orders(status: str = "all") -> list[dict]` — `GET /v2/orders` with `status`, `nested=true`, `limit=500`, `direction=desc`, paged by `until`; keeps parents whose `client_order_id` is app-owned (children come nested).
  - `async def app_net_positions() -> dict[str, int]` — signed sum of `filled_qty` over app orders and their filled children (buy +, sell −; for mleg sum each leg by `ratio_qty * filled_qty`), keys `occ_unpadded(symbol)`, zeros dropped.
  - `async def account_positions() -> dict[str, dict]` — `GET /v2/positions` keyed by `occ_unpadded(symbol)`, values `{"qty": int (signed), "avg_entry_price": float, "asset_class": str}`.
- Produces (`account.py`, same names as today): `balances() -> dict` (keys unchanged; `net_liquidation←equity`, `total_cash←cash`, `buying_power←buying_power`, `available_funds←buying_power`, `account` masked, others `None`), `positions() -> list[NormalizedPosition]` (app-owned only: symbols from `app_net_positions()`, qty from ledger, `avg_price` from `account_positions()` when present else 0.0; `instrument_type` `"Equity Option"` for `asset_class == "us_option"` else `"Equity"`; `account` masked), `live_orders() -> list[dict]` (app-owned open orders, `app_orders("open")`), `cancel_order(order_id: str) -> bool` (looks the id up among app-owned open orders first; untagged → `False`, no DELETE sent; the DELETE goes through `request(..., write=True)` so the paper lock applies). Remove `get_tt_account`/`reset_tt_account` and update their callers (`app/auth/routes.py` account display → Alpaca masked account or `""`).
- All reads degrade to `{}`/`[]`/`False` on `AlpacaError`/`httpx.HTTPError` (log warning), as today.

- [ ] **Step 1: Write failing tests** (`test_ledger.py`, `test_broker_account_alpaca.py`):
  - `test_ledger_ignores_untagged_orders`: orders `tsys-a` buy 10 filled + `manual-1` buy 50 filled on AAPL → `{"AAPL": 10}`.
  - `test_ledger_uses_filled_qty_for_partial_fills`: `tsys-a` buy qty 10, filled_qty 4 → `{"AAPL": 4}`.
  - `test_ledger_bracket_children_net_out`: tagged bracket parent buy 5 filled, nested take-profit sell 5 filled → `{}`.
  - `test_ledger_option_symbols_unpadded`: tagged order symbol `QQQ261117C00660000` buy 1 → key `QQQ261117C00660000`.
  - `test_positions_only_app_owned_with_avg_price`: account positions AAPL 60 (avg 190.0) and TSLA 3; ledger AAPL 10 → one `NormalizedPosition(symbol="AAPL", quantity=10, quantity_direction="Long", instrument_type="Equity", avg_price=190.0, …)`.
  - `test_cancel_order_refuses_untagged`: open orders contain id `o-manual` with client id `manual`; `cancel_order("o-manual")` → `False` and no DELETE request recorded.
  - `test_balances_masked_and_mapped`: `/v2/account` equity `"1000.5"`, account_number `"PA12345678"` → `net_liquidation == 1000.5`, `account == "••••5678"`.
  - `test_reads_degrade_on_error`: 500 everywhere → `balances() == {}`, `positions() == []`, `live_orders() == []`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `ledger.py` and rewrite `account.py`.
- [ ] **Step 4: Run** `pytest tests/test_ledger.py tests/test_broker_account_alpaca.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(broker): app-owned Alpaca ledger and account views`.

---

### Task 4: Alpaca order submission, cancel, fill polling

**Files:**
- Modify (rewrite internals, keep public names/return shapes): `backend/app/broker/orders.py` — `submit_equity_order`, `submit_bracket_order`, `submit_combo_order`, `cancel_symbol_orders`, `poll_bracket_fill`; keep `intent_to_action`, `occ_symbol_for` (now returns unpadded OCC). Remove all `tastytrade` imports, `build_otoco`, `_validated_place`, `_new_order`, `_order_action`, `_equities`, `_option`.
- Modify: `backend/app/engine/order_store.py` (no signature change; reuse `record_intent`, `mark_perm_id`)
- Delete/replace tests: `test_broker_equity_orders.py`, `test_broker_bracket.py`, `test_broker_combo.py`, `test_combo_execution.py`, `test_combo_quantity.py`, `test_options_combo.py`, `test_execution_ibkr.py`, `test_submit_idempotent.py`, `test_worker_transmit_guard.py`, `test_live_orders_switch.py` → rewrite the ones whose behaviour still applies against Alpaca in `backend/tests/test_orders_alpaca.py`; keep worker-level tests that only monkeypatch `app.engine.execution` functions.
- Test: `backend/tests/test_orders_alpaca.py`

**Interfaces:**
- Consumes: Task 2 client; Task 3 `app_orders`, `app_net_positions`.
- Produces (unchanged shapes): equity/bracket/combo return dicts with `transmitted`, `detail`, plus `order_id` (equity), `parent_order_id` + `stop` + `target` + `bracket: True` (bracket), `legs` (combo), `ref` (all transmitted submits), `paper: True` on gate-OFF paths, `safe_mode: True` when blocked.
- Check order on every transmit path: qty → safe mode → gate → paper lock (`trading_base()`; on `PaperLockError` return the Global-Constraints blocked dict) → `record_intent(...)` → POST with `client_order_id=tag(ref)` → `mark_perm_id(ref, alpaca_id)`.
- Bodies (exact Alpaca fields): equity `{"symbol","qty":str,"side":"buy|sell","type":"market","time_in_force":"day","client_order_id"}`; bracket adds `"order_class":"bracket","time_in_force":"gtc","take_profit":{"limit_price":str(target)},"stop_loss":{"stop_price":str(stop)}`; combo `{"order_class":"mleg","qty":str(quantity),"type":"limit"|"market","limit_price":str(abs(net)) (only if net_price given),"time_in_force":"day","legs":[{"symbol":occ,"ratio_qty":"1","side":"buy|sell","position_intent":"buy_to_open|buy_to_close|sell_to_open|sell_to_close"}],"client_order_id"}` where closing = the app's own net position in that OCC is on the opposite side.
- `cancel_symbol_orders(symbol) -> dict`: gate OFF → `{"cancelled": 0, "paper": True}`; else cancel app-owned open orders whose symbol or any leg symbol matches (`DELETE /v2/orders/{id}`), then poll `app_orders("open")` up to 5s (0.25s steps) until none match; returns `{"cancelled": n}` (+ `"pending": True` if still open after the window).
- `poll_bracket_fill(symbol, ref) -> dict | None`: gate OFF → `None`; find the app bracket parent by `ref` (stored `parent_order_id`) else the latest app bracket for `symbol`; `None` if no exit child is `filled`; else `{"price": float(filled_avg_price), "ms": <filled_at epoch ms>, "reason": "stop" | "target"}` (`type == "stop"` → stop, `"limit"` → target); lookup error → `{"price": None, "ms": None, "reason": "error"}`.

- [ ] **Step 1: Write failing tests** in `test_orders_alpaca.py` (record requests with MockTransport):
  - `test_equity_gate_off_simulates_without_network` (existing behaviour: mid fill via monkeypatched `_mid`; handler never called).
  - `test_equity_safe_mode_blocks_before_network`.
  - `test_equity_paper_lock_blocks_live_host`: live base URL, gate ON → `transmitted False`, detail `"blocked: Alpaca live endpoint locked (paper only)"`, no request, no order-intent row.
  - `test_equity_transmits_tagged_market_order`: body matches exact fields; `client_order_id.startswith("tsys-")`; result `transmitted True`, `order_id` = Alpaca id, `ref` present; intent row has `perm_id`.
  - `test_equity_rejection_returns_reason`: 422 `{"message":"insufficient buying power"}` → `transmitted False`, detail contains message.
  - `test_bracket_body_and_shape`: exact bracket fields; returns `parent_order_id`, `stop`, `target`, `bracket True`.
  - `test_combo_rejects_more_than_four_legs`: 6 legs → `transmitted False`, detail mentions `4`, no request.
  - `test_combo_position_intent_from_app_ledger`: ledger short 1 on leg A (app) and manual long 5 on leg A in account → buying leg A is `buy_to_close`; leg B with no app position → `*_to_open`.
  - `test_cancel_symbol_orders_skips_untagged_and_waits`: open orders `tsys-1` (AAPL) + `manual` (AAPL) → only `tsys-1` DELETEd; poll sees it gone → `{"cancelled": 1}`.
  - `test_poll_bracket_fill_stop_and_target`: nested stop child filled at 98.5 → `reason "stop"`, price 98.5; no child filled → `None`.
  - `test_orders_module_has_no_tastytrade_import`: `"tastytrade" not in inspect.getsource(app.broker.orders)` and `not in getsource(app.broker.account)`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** in `orders.py`.
- [ ] **Step 4: Run** `pytest tests/test_orders_alpaca.py -q` then full suite `pytest -q` → PASS (delete/rewrite superseded tests listed above as part of this step).
- [ ] **Step 5: Commit** `feat(broker): route orders to Alpaca paper with ownership tags`.

---

### Task 5: App-scoped kill-switch flatten

**Files:**
- Modify: `backend/app/broker/orders.py::flatten_all`
- Delete/replace: `backend/tests/test_broker_flatten.py`, `backend/tests/test_kill_flatten_ledger.py` (keep any assertions still valid for `close_open_trades`) → `backend/tests/test_flatten_alpaca.py`

**Interfaces:**
- Consumes: Task 3 `app_net_positions`, `account_positions`, `live_orders`, `cancel_order`; Task 4 submit body helpers.
- Produces: `async def flatten_all() -> str` — same contract (returns summary, raises `RuntimeError("Flatten incomplete: …")` when anything app-owned remains or on gate OFF with app positions).
- Live algorithm: cancel all app-owned open orders → for each `(sym, app_qty)` in `app_net_positions()`: `close_qty = min(abs(app_qty), abs(account_qty))` if account position has the same sign, else 0 (record failure "account position differs"); submit tagged market close (`side` opposite; options use `sell_to_close`/`buy_to_close` via a single-leg order, `time_in_force "day"`) → poll `app_net_positions()` up to 10s (0.5s steps) → raise if any non-zero remain (message lists them; mention "orders may be queued until market open").

- [ ] **Step 1: Write failing tests** in `test_flatten_alpaca.py`:
  - `test_flatten_closes_only_app_qty_when_manual_shares_share_symbol`: account AAPL +60, ledger AAPL +10 → exactly one POST: AAPL sell qty `"10"`, tagged; after poll ledger 0 → returns string containing `"AAPL x10"`.
  - `test_flatten_never_calls_bulk_endpoints`: assert no request to `DELETE /v2/positions` or `DELETE /v2/orders` (without id).
  - `test_flatten_cancels_only_app_orders`: open `tsys-1`, `manual` → only `tsys-1` cancelled.
  - `test_flatten_gate_off_with_app_positions_raises_and_sends_nothing`: gate OFF, ledger AAPL +10 → `RuntimeError` mentioning `AAPL`, zero POST/DELETE requests.
  - `test_flatten_reports_not_flat_when_fill_pending`: ledger stays +10 after close → `RuntimeError` containing `"queued"`.
  - `test_flatten_paper_lock`: live host, gate ON → raises, no write requests.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `flatten_all`.
- [ ] **Step 4: Run** `pytest tests/test_flatten_alpaca.py tests/test_kill_switch.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(broker): kill switch flattens only app-owned Alpaca positions`.

---

### Task 6: Reconciliation against transmitted trades only

**Files:**
- Modify: `backend/app/db/models.py::TradeRecord` (add `transmitted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)`)
- Modify: `backend/app/db/base.py` migrations (`ALTER TABLE trades ADD COLUMN transmitted BOOLEAN DEFAULT 0`)
- Modify: `backend/app/engine/worker.py::_open_trade` (+ `transmitted: bool = False` param) and its two call sites (~lines 185, 375) pass `bool(order.get("transmitted"))`
- Modify: `backend/app/engine/recovery.py::local_open_positions` (filter `transmitted`), `run_recovery` (broker-unavailable handling)
- Modify: `backend/app/engine/reconciliation.py::fetch_snapshot` (positions from `app_net_positions()`; add account-smaller check), `reconcile` (signature unchanged)
- Delete/replace: `test_broker_reconciliation.py`, `test_recon_matrix.py`, `test_recovery_sequence.py`, `test_recovery_backfill.py` (update to new sources)
- Test: `backend/tests/test_recon_alpaca.py`

**Interfaces:**
- Consumes: Task 3 ledger.
- Produces: `fetch_snapshot() -> dict` with keys `positions: list[BrokerPosition]` (app-owned, `avg_price` from account positions), `fills: []`, `orders: list[BrokerOrder]` (app-owned open), and new `issues: list[str]` (e.g. `"AAPL: account 5 < app-owned 10"`); raises `BrokerUnavailable` (new exception in `reconciliation.py`) when the ledger/positions fetch fails.
- `run_recovery()`: on `BrokerUnavailable` → `trip_safe_mode("broker unavailable: Alpaca")` and return False; otherwise discrepancies = `reconcile(...)` + `snap["issues"]`.

- [ ] **Step 1: Write failing tests:**
  - `test_simulated_open_trade_does_not_trip`: open `TradeRecord(transmitted=False)` AAPL buy 10; ledger `{}` → `run_recovery()` True, safe mode off.
  - `test_transmitted_open_trade_matches_ledger`: transmitted AAPL buy 10; ledger `{"AAPL": 10}` → True.
  - `test_untagged_account_positions_ignored`: account TSLA 100, ledger `{}`, no local trades → True.
  - `test_account_smaller_than_app_owned_trips`: transmitted AAPL 10; ledger 10; account AAPL 5 → False, reason contains `"account 5 < app-owned 10"`.
  - `test_recovery_trips_with_broker_unavailable_reason`: Alpaca 500 → False, safe-mode reason starts `"broker unavailable"`.
  - `test_worker_marks_transmitted`: call `_open_trade(..., transmitted=True)` → row `transmitted is True`; default False.
  - `test_migration_adds_transmitted_column`: create old `trades` table without the column, run migrations → column exists, existing rows read as False.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** model/migration/worker/recovery/reconciliation changes.
- [ ] **Step 4: Run** full suite `pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(engine): reconcile only transmitted trades against the app's Alpaca ledger`.

---

### Task 7: Readiness gating + UI copy

**Files:**
- Modify: `backend/app/services/alpaca_status.py` (add `def alpaca_ready_cached() -> bool` = last cached status is `"ok"`; refresh it from the existing `broker.status.refresher` loop or a sibling task started in `main.py` lifespan every 60s)
- Modify: `backend/app/routers/dashboard.py::account`, `::positions`; `backend/app/routers/ws.py` `_positions_snapshot`/`_account_snapshot` — gate on `alpaca_ready_cached()` instead of `session_manager.is_authenticated()`; fix docstrings/notes that say IBKR/Tastytrade account
- Modify: `frontend/src/components/DataBrokerChips.jsx` — `Broker: Alpaca paper · ok|not connected` from `api.alpacaStatus()`; Orders chip `Simulated` (OFF) / `Alpaca paper` (ON)
- Modify: `frontend/src/components/ApiTab.jsx` — first card title `Market data — Tastytrade` (drop Net liq/Buying power there); Alpaca card is the broker card; Order Routing copy: OFF "Simulated — orders never leave the app", ON "Orders go to your Alpaca **paper** account (live trading locked)"; arm button text `Send orders to Alpaca paper…`
- Modify: `frontend/src/components/Dashboard.jsx` — Account card subtitle `Alpaca paper · shared account`; empty text `Alpaca not connected.`; Open Positions heading note `app-owned only`
- Test: `backend/tests/test_alpaca_gating.py`

**Interfaces:**
- Consumes: Task 3 `balances`, `positions`; Task 2 `configured`.
- Produces: `alpaca_ready_cached() -> bool`.

- [ ] **Step 1: Write failing tests:**
  - `test_account_endpoint_uses_alpaca_readiness`: Tastytrade probe not ok, Alpaca cached ok, monkeypatched `balances` → `/api/dashboard/account` (path as mounted) returns the balances (with app token header set via `APP_AUTH_TOKEN` monkeypatch).
  - `test_account_endpoint_when_alpaca_down`: Alpaca not ok → `{"account": {}, "note": "Alpaca not connected."}`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** backend gating and frontend copy.
- [ ] **Step 4: Run** full suite `pytest -q` → PASS; `cd frontend && npm run build` → `✓ built`; `grep -rn "IBKR\|Tastytrade verified" frontend/src` → none.
- [ ] **Step 5: Commit** `feat(ui): Alpaca paper as broker, Tastytrade as data; simulated vs paper order labels`.

---

### Task 8: End-to-end verification

- [ ] **Step 1:** Full suite from `backend/`: `pytest -q` → all pass.
- [ ] **Step 2:** `grep -rn "place_order\|place_complex_order\|delete_order\|get_live_orders\|get_positions" backend/app` → no Tastytrade order/position calls remain.
- [ ] **Step 3:** Restart the backend via the `backend` preview config; `/health` ok; `/api/auth/alpaca` status `ok`; dashboard shows `Data: Tastytrade`, `Broker: Alpaca paper`, `Orders: Simulated`; Strategies list has no Triple Calendar; Journal shows the two old TC rows as closed.
- [ ] **Step 4 (needs the user's go-ahead — it places a real order on the shared paper account):** with `ENABLE_LIVE_ORDERS=true` and a test-only call, submit 1 share of a liquid equity, confirm it appears tagged `tsys-` in Alpaca, run flatten, confirm only that share closed and untagged holdings unchanged, set `ENABLE_LIVE_ORDERS=false`.
- [ ] **Step 5:** Note for the user: safe mode is still on from the Tastytrade mismatch; set `APP_AUTH_TOKEN`, then Resume once to re-reconcile against Alpaca.
