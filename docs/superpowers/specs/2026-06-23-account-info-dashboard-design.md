# Account Info on Dashboard — Design Spec

Date: 2026-06-23
Status: Approved — implement.

## Goal
Show the IBKR account summary (full balances + P&L + margin) on the dashboard, live.
Read-only, single (paper) account, no historical chart.

## Fields (full summary)
`net_liquidation`, `total_cash`, `buying_power`, `available_funds`, `gross_position_value`,
`unrealized_pnl`, `realized_pnl`, `init_margin`, `maint_margin`, plus `account` and `currency`.

## Architecture

### Backend — `app/ibkr/account.py`
- **`normalize_account_summary(values) -> dict`** (pure, testable): maps IBKR
  `accountSummary` rows (objects with `.tag`, `.value`, `.currency`, `.account`) to our keys
  via a tag map, parsing values to float. Missing tags default to `None`.
  Tag map: `NetLiquidation→net_liquidation`, `TotalCashValue→total_cash`,
  `BuyingPower→buying_power`, `AvailableFunds→available_funds`,
  `GrossPositionValue→gross_position_value`, `UnrealizedPnL→unrealized_pnl`,
  `RealizedPnL→realized_pnl`, `InitMarginReq→init_margin`, `MaintMarginReq→maint_margin`.
  Result also carries `account` (first row's account) and `currency` (first row's currency).
- **`async get_account_summary(account=None) -> dict`**: if the gateway isn't connected,
  return `{}`; else `rows = await ib.accountSummaryAsync(account or "")` → normalize.

### Exposure
- **WS snapshot** (`app/routers/ws.py`): add a new `"account"` key to the snapshot dict,
  populated under the same authenticated guard as `positions` (empty `{}` when not
  connected). Streams at the existing 2s cadence.
- **REST** (`app/routers/dashboard.py`): `GET /api/account` (gated by `require_auth`) →
  `{"account": <summary or {}>, "note": <str|None>}`, degrading like `/api/positions`.

### Frontend — `frontend/src/components/Dashboard.jsx`
- New **Account** card (reuse the existing `card` + `deploy-grid` stat styling, indigo
  accent) reading `snapshot.account`. Currency-format numbers; color `unrealized_pnl` /
  `realized_pnl` green when ≥0, red when <0. Show "—" for null fields and a "Connect IBKR"
  hint when the summary is empty.

## Error handling
Disconnected or transient IBKR error → empty `{}` summary → card renders placeholders;
never throws / never 500s (matches positions/overview degradation).

## Testing
- **Unit (TDD):** `normalize_account_summary` with stub rows — correct tag→key mapping,
  float parsing, `account`/`currency` carried, missing tags → `None`, empty input → all-None
  dict.
- **Live:** `curl /api/account` against the running connected app → real balances for the
  paper account (DUO710257).

## Out of scope
Withdrawals/transfers, multi-account switching, historical equity/P&L charting, per-strategy
account allocation (already shown elsewhere).

## Notes
Repo under git. Spec on disk.
