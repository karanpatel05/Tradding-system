# Builder — visible option-chain grid — design

**Date:** 2026-07-05
**Status:** Approved (brainstorm) → implementation

## Goal

Make the option chain visible in the Builder tab: show **all available expiries** (as chips
with DTE) and, for the selected expiry, a **quoted grid of strikes near ATM** (call mid /
strike / put mid). Tapping a price adds that option as a leg. Today the chain data
(expirations + strikes) is only reachable through the per-leg dropdowns and a "N expiries · M
strikes" summary; this surfaces it.

## Constraints

- **Quotes are expensive:** each option mid is a ~2s IBKR `reqMktData` fetch. A full chain
  (hundreds of strikes × call+put) is infeasible. So the grid shows a **window of `count`
  strikes on each side of ATM** (default 8 → ~17 strikes → ~34 mids), fetched **concurrently**
  (`asyncio.gather`) so it loads in a few seconds, not linearly.
- Mids are **`null`** when unavailable (deep OTM, market closed) → shown as `—`.
- Read-only: this only fetches quotes/definitions; it never places orders (the Builder's
  no-transmit invariant is unaffected).

## Backend

**New read-only helper — `app/options/chain.py::get_chain_quotes(underlying, expiry, count=8)`**
(async). To make it unit-testable, import `get_quotes` at module top in `chain.py` and call
the module-level `get_option_chain` (both monkeypatchable).
1. `spot = (await get_quotes([underlying]))[underlying]["price"]`.
2. `strikes = (await get_option_chain(underlying))["strikes"]` (sorted floats).
3. If `spot` is unavailable or no strikes → return `{underlying, expiry, spot, atm: None, count,
   rows: []}`. **"Unavailable" means `spot is None` OR `spot <= 0`** — IBKR returns `-1` (a
   sentinel), not `None`, when the underlying has no live quote (e.g. market closed / no
   subscription). Do not treat `-1` as a real price.
4. `atm = min(strikes, key=lambda s: abs(s - spot))`; take the index window `[i-count, i+count]`
   (clamped to bounds) → `window` (up to `2*count+1` strikes).
5. Concurrently fetch call+put mids: build tasks `aoption_mid(underlying, date(expiry), s, "C")`
   and `... "P"` for each `s` in `window`; `await asyncio.gather(*tasks)`.
6. Return `{underlying: UPPER, expiry, spot, atm, count, rows: [{strike, call_mid, put_mid}]}`
   (rows in ascending strike order; a mid may be `null`).

**New endpoint — `app/routers/builder.py`:**
`GET /api/builder/chain-quotes?underlying=SYM&expiry=YYYY-MM-DD&count=8` → `get_chain_quotes`.
Clamp `count` to a sane range (1–25) to bound the quote fan-out.

## Frontend — a "Option chain" card in `BuilderTab.jsx`

Rendered once a chain is loaded (`chain.expirations` non-empty). Dark theme, indigo accent,
matching the mockup.

1. **Header:** `Option chain — <underlying>` + a `spot <price>` badge (from the last grid load)
   + a **strikes ±count** control (select: 6 / 8 / 12 / 20; default 8).
2. **Expiry chips (all of them):** every `chain.expirations` entry as a chip showing month-day
   + DTE (`Jul-18 · 14d`, DTE = expiry − today). The selected chip is highlighted (indigo).
   Selecting a chip loads its grid.
3. **Quoted grid:** a 3-column table `Call mid | Strike | Put mid`, one row per window strike,
   the **ATM row highlighted**, `—` for null mids. Call/put price cells are tappable.
4. **Tap to add a leg:** tapping a **call** price appends a `BUY / Call / <strike> / <expiry> /
   qty 1` leg; a **put** price appends a `BUY / Put / ...` leg. Reuses the existing legs state
   and **invalidates the fetched net-debit/payoff** (same as any leg edit, per the existing
   `invalidateQuote`). Side/qty are then editable in the legs table as today.
5. **Refresh** button re-pulls the current expiry's grid (snapshot; no auto-poll — too costly).
6. **Loading / empty states:** "Loading quotes…" while fetching; if `spot`/rows are empty,
   a muted "No quotes (market closed or unavailable)" note.

`frontend/src/api.js`: add `builderChainQuotes(underlying, expiry, count)` →
`GET /api/builder/chain-quotes?...` via the existing `request` helper.

The existing per-leg strike/expiry dropdowns stay (manual editing path); the chain card is an
additional, faster way to add legs.

## Testing

- **Backend** (`test_builder_chain_quotes.py`): monkeypatch `chain.get_quotes` (spot),
  `chain.get_option_chain` (a fixed sorted strike list), and `LivePriceSource.aoption_mid`
  (deterministic mids, one `None` inside the window). Assert: correct `atm` (nearest strike to
  spot), window size `2*count+1` and exact strikes, each row has `call_mid`/`put_mid`, the
  `None` mid surfaces as `None`, and rows are strike-ascending. Also a boundary case
  (ATM near the top of the strike list → window clamps, fewer rows, no error).
- **Frontend:** verified live — load chain, tap an expiry chip → grid loads with call/put mids
  and the ATM row highlighted; tap a call price → a BUY call leg appears in the legs table;
  Refresh re-pulls. (Market-closed → mids may show `—`, which is expected.)

## Non-goals (v1)

- No bid/ask columns (single mid per side); no greeks/IV/volume/OI in the grid.
- No full-chain (all strikes) quotes; only the ±count ATM window.
- No auto-refresh/streaming of the grid.
- Tap adds a BUY leg (side edited in the legs table) — no per-cell buy/sell toggle.
