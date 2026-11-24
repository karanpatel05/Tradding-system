# Live trade recorder for the chart store — design

Date: 2026-11-24 · Status: draft for approval
Builds on: `feat/chart-history` (spec `2026-11-20-chart-history-design.md`, unmerged).

## Why

The Chart was slow because every candle request went through the Tastytrade candle
collector (~4.3 s per symbol, one symbol at a time). `feat/chart-history` already fixes
history: a local SQLite store of Alpaca SIP 1-minute bars (`market_history.db`) for SPY,
QQQ, TSLA and NVDA back to 2022, topped up every minute during market hours, plus
scroll-back and go-to-date. Its live edge pushes only a last-trade price to the browser,
which folds it into the newest candle; nothing live is saved and live volume is not shown.

The user now wants the moving edge to be real stored data too: **during regular trading
hours, save live trades temporarily, roll them into 1-minute bars, show those live, and
delete the trade-level data at the end of the day.** Outside regular hours the chart shows
stored data only.

Success: for a listed symbol during RTH, the chart's newest minutes (including the forming
minute, with volume) come from the server and survive a page reload or a service restart;
the day's history afterwards matches the official consolidated bars; no trade-level data
is left after the day ends.

## Decisions the user made

- **Build on `feat/chart-history`**, not a separate store.
- **A fixed list of symbols** is recorded live, set in config.
- **Trades are kept temporarily, rolled into 1-minute bars, and deleted at the end of
  the day.** This reverses the Nov 20 chart-history decision "raw ticks are never saved".
- **Live data only during regular trading hours** (09:30–16:00 ET, NYSE trading days).
- **Official bars win**: where trade-built minutes and the official 1-minute bars differ,
  the official ones are kept. (The user accepted this default without objection.)
- Storage choice from the earlier draft (a market-data file separate from `trading.db`)
  is already satisfied by `market_history.db`.

## Goals

1. During RTH, a server-side recorder captures every valid regular-session trade for the
   listed symbols, saves it, and maintains 1-minute bars (OHLCV) for the current day,
   including the forming minute.
2. The history API and the chart's live edge serve those minutes for the period the Alpaca
   store has not caught up to yet, so the chart is complete up to the current second.
3. A mid-day restart rebuilds the day's trade-built minutes from saved trades.
4. The official Alpaca SIP bars replace trade-built minutes as they arrive; at the end of
   the day every RTH minute is official (or visibly missing), and the day's trades are
   deleted.
5. Trade-level data never survives past the day it belongs to, including after a missed
   end of day (Mac asleep, service down).

## Non-goals

- Extended-hours live data or trade recording.
- SPX/NDX (cash indices have no trades; they keep chart-history's behaviour).
- Symbols outside the list (they keep chart-history's fallback path).
- Strategies reading this store; changing the Tastytrade candle collector.
- A UI to edit the list.

## Anti-goals

- The recorder slowing the event loop, the shared DXLink feed, live strategies or order
  paths. A recorder failure must never crash the service or affect trading.
- Trade-built minutes overwriting official bars, or being shown as official.
- Trade-level data accumulating across days.
- A chart that looks live with stale data (keep chart-history's 120 s max trade age rule).
- Two processes recording at once.

## Constraints

- Everything in the chart-history spec still applies (Alpaca SIP source, split
  adjustment, `MARKET_HISTORY_SYNC` single-writer gate and lock file, test guard against
  the real file, `run_live.sh` exports, service runs from the GitHub `main` worktree and
  restarts with `launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web`).
- Shipping requires `feat/chart-history` + this work merged to GitHub `main`. A trial
  merge of `feat/chart-history` into `origin/main` (3d65368) on 2026-11-24 was clean.
- Trades come from DXLink `TimeAndSale` (fields include `time`, `price`, `size`,
  `extended_trading_hours`, `valid_tick`, `type`, `sequence`). Heavy symbols can print
  hundreds of thousands to over a million trades a day; writes must be batched.
- Alpaca SIP bars are split-adjusted; live trades are raw. They only differ on a split
  day, which the store's split check already re-downloads.

## Approaches considered

1. **Trade recorder writing into `market_history.db`, merged at read time** — *chosen.*
   A `ticks` table and a `live_1m` table sit next to `bars_1m`. Reads take official
   minutes from `bars_1m` and fill anything newer from `live_1m`. Official bars win by
   construction; the end-of-day job only checks, finalizes and deletes.
2. **Recorder writes trade-built minutes straight into `bars_1m`**, later overwritten by
   Alpaca. Fewer tables, but official and unofficial rows become indistinguishable, and a
   failed Alpaca top-up would leave trade-built minutes posing as official.
3. **Keep the browser-side live candle, add a server tick log only for restarts.**
   Smallest change, but the live candle still has no volume and differs per browser.

## Design

### Symbol list

- `LIVE_BAR_SYMBOLS` in `backend/.env`. *Claude's call:* it also becomes the list the
  Alpaca store syncs (replacing the hard-coded `STOCK_SYMBOLS`), so every recorded symbol
  has history behind it; one list, not two. Default `SPY,QQQ,TSLA,NVDA` (chart-history's
  user-chosen stocks). Adding a symbol triggers the store's normal backfill from
  2022-01-03. Index symbols in the list are ignored with a logged warning.

### Storage (in `market_history.db`)

- `ticks(symbol, ts_ms, seq, price, size)` — today's valid regular-session trades for
  listed symbols; key `(symbol, ts_ms, seq)`. Temporary.
- `live_1m(symbol, ts, open, high, low, close, volume, trades)` — trade-built minutes for
  the current day; same bar shape as `bars_1m`. Rebuildable from `ticks`.
- `bars_1m`, `bars_1d`, `sync_state` unchanged.

### Recorder (`app/history/recorder.py`, started with the sync job, same gate)

- Runs only when `MARKET_HISTORY_SYNC` is on (the live service) and holds the store lock.
- Subscribes to `TimeAndSale` for the listed symbols just before 09:30 ET on trading days;
  unsubscribes after 16:00 ET. No subscription outside RTH.
- Keeps trades with `extended_trading_hours` false and `valid_tick` true whose time falls
  in 09:30:00.000–16:00:00.000 ET; the 16:00 closing print folds into the 15:59 minute.
  Cancel/correction handling per `type` is the builder's to confirm against live data.
- Resubscribes after a DXLink drop (chart-history already resubscribes chart symbols on
  reconnect; use the same hook).
- Buffers in memory, flushes about once a second in one transaction (insert ticks, upsert
  touched `live_1m` rows). The forming minute is also held in memory for the live push.
- On start during RTH: rebuild `live_1m` for today from `ticks`, then record.
- Errors are logged and retried with backoff; database work never blocks the loop.

### Reads (history API and `/ws` live edge)

- `GET /api/market/history` for a listed symbol: official minutes from `bars_1m`; for
  minutes after the newest official minute (today only), take `live_1m`; aggregates
  (5m … 1h) are built from the merged minutes; `1D` for today, during RTH, is built from
  today's merged minutes. Response gains a `live_from` timestamp marking where
  trade-built minutes begin (for the chart to style or ignore).
- `/ws` live edge for listed symbols: push the forming minute's OHLCV (and the just-closed
  minute) at most once per second, replacing the last-trade-price push for these symbols.
  Non-listed chart symbols keep the last-trade push. The frontend uses the server's bar
  instead of folding prices itself (`liveCandle.js` simplifies to "replace or append").

### Official bars win

- The store's Alpaca top-up (every minute during RTH, final pass after 20:15 ET) writes
  official minutes into `bars_1m`. Because reads prefer `bars_1m`, any trade-built minute
  is superseded the moment its official bar exists.

### End of day

After the store's final top-up pass (20:15 ET) on a trading day:

1. Confirm every RTH minute of the day exists in `bars_1m`. Minutes still missing are
   re-requested from Alpaca once; if still missing, they stay missing (logged in
   `sync_state.note`), never filled from trade-built data.
2. Delete that day's rows from `ticks` and `live_1m`.
3. On startup, any `ticks`/`live_1m` rows from an earlier day are handled the same way
   before recording starts (catches a missed end of day).

The file keeps freed pages for the next day; no VACUUM.

### Frontend

- `liveCandle.js` / `marketData.js`: for listed symbols, apply the server's forming bar
  from `/ws`; outside RTH no live updates (unchanged). Coordinate with the FRONTEND
  session, which owns the chart UI.

## What's left to the builder

- SQLite pragmas, batch size, flush interval within "about once a second".
- Exact `TimeAndSale` filtering for cancels/corrections (confirm with live data).
- How `live_from` is shown (or not) on the chart.
- **First task:** on a live session, confirm `TimeAndSale` events arrive for the listed
  symbols with this account and measure trades/day per symbol. If they don't, stop and
  bring the alternative (building minutes from `Trade` events and `day_volume` deltas)
  back for a decision.
- Branching: work on `feat/live-tick-bars` off `feat/chart-history`; land
  `feat/chart-history` on `main` first (or together), coordinated with the BACKEND
  session, which wrote it.

## Testing

- Recorder with a fake `TimeAndSale` stream: RTH filter, extended-hours and invalid ticks
  dropped, minute bucketing across boundaries and the 16:00 print, batching, restart
  rebuild from `ticks`, resubscribe after drop.
- Read merge: official minutes win over `live_1m`; `live_from`; aggregates across the
  official/live seam; today's partial `1D`.
- End of day: missing-minute retry, deletion, catch-up of earlier days on startup.
- No test touches the network, the real `market_history.db`, or `trading.db`.
