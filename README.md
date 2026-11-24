# Multi-Strategy Automated Trading System (Alpaca paper orders, Tastytrade data)

Runs trading strategies with **orders routed to an Alpaca paper account** (paper-locked in
code; live Alpaca is a deliberate future code change) and **Tastytrade for market data
only** (quotes, option chains, history; OAuth personal grant). The Alpaca paper account
may be shared with other tools: the app only touches orders it tagged (`tsys-`
client_order_id). Includes a live engine, dashboard, journal, equity backtests and an
options Builder (paper-tracked, never places orders). The Strategies tab lists the QQQ
first-hour credit spread, which runs outside this app and is switched off here (see
"Strategies" below); the other slots are Coming Soon. Runs as a local web app, a macOS
LaunchAgent service, or a hosted Docker stack.

## Architecture

```
backend/   FastAPI + async SQLAlchemy + httpx (Alpaca) + tastytrade SDK (data)
  app/
    config.py            env-driven settings (APCA_* Alpaca keys, Tastytrade OAuth creds)
    auth/                connection-state auth (authenticated == Tastytrade OAuth probe verifies)
    broker/              Alpaca paper orders (paper lock, tsys- tag), app-owned ledger, account
    engine/              kill-switch task registry, per-strategy workers, execution, recovery
    strategies/          BaseStrategy + registry (QQQ credit spread listed, switched off;
                         other slots Coming Soon)
    backtest/            pure-Python metrics + historical runner
    services/            market status/overview, logging + journal/calendar
    routers/             dashboard, strategies, journal, websocket
    db/                  models + async engine (SQLite dev / Postgres prod)
  tests/                 kill switch, journal, strategy gating, worker, backtest
frontend/  React + Vite + Recharts — the operator console (see "Frontend" below)
  src/
    App.jsx              shell: navigation rail, toolbar, emergency stop, banners
    components/          Dashboard, StrategiesTab/StrategyDetail, BuilderTab (+ builder/),
                         CalendarTab (journal), ApiTab (connections), shared widgets
    operator.css         operator console design system (layout, panels, tables, charts)
    redesign/            standalone sample-data concept preview (never calls the API)
docker-compose.yml       postgres + backend (serves the built UI) + caddy
```

## Frontend: operator console

![Operator console overview (concept preview with sample data)](frontend/redesign-preview/overview.jpg)

<sub>Screenshot of the concept preview (`redesign.html`, sample data). The real app uses the
same layout, filled with live data from the API.</sub>

The UI is a dark navigation rail with a light workspace. A persistent toolbar shows feed
status, paper/simulated order mode, the data and broker sources, and the **Emergency
stop**. Recovery (safe mode) and connection banners appear above every screen.

| Screen | What it shows |
| --- | --- |
| **Overview** | Market board, Alpaca paper account metrics, app-owned positions, strategy activity, allocation budgets, system health |
| **Strategies** | Strategy table plus detail, allocation, activation and backtest workflows |
| **Options builder** | Analysis workspace (quote, payoff, Greeks, IV smile) and a trade setup panel with templates and stock/option legs; **Build \| Trades** toggle for paper-tracked trades and portfolio P&L |
| **Journal** | Month calendar of sessions with per-day session detail |
| **Connections** | Tastytrade market data, Alpaca paper, access token and order-routing settings |

Notes:

- **Real data only.** The main app shows no sample figures or mock charts. Missing account
  values show as dashes, and stale snapshots are labelled. Allocation bars are strategy
  budgets, not measured risk.
- **The UI is not the safety layer.** Order routing, activation, resume and emergency stop
  keep their confirmation dialogs, and the backend still enforces every check.
- **The access token** is entered in Connections and stored in the git-ignored
  `backend/.env`. It is never bundled into the frontend.
- **Responsive:** each main screen works at 390px width without horizontal scrolling.

### Concept preview (sample data)

`frontend/redesign.html` is a standalone, sample-data version of the design. It runs on
browser-local state, resets on reload and never calls the broker or app APIs. Screenshots
of all five screens are in `frontend/redesign-preview/`.

```bash
cd frontend
npm run dev -- --host 127.0.0.1 --port 5174   # open http://127.0.0.1:5174/redesign.html
npx vite build --config redesign.config.js    # builds to frontend/dist-redesign
```

## Key guarantees

- **Auth == Tastytrade OAuth probe verifies** — "authenticated" means the token-refresh
  probe against Tastytrade succeeds (see `app/broker/status.py`); the gated REST/WebSocket
  routes pass while it does. There is **no per-user login**. This is fine for a local
  browser tool, but anything network-exposed MUST sit behind the Docker stack's Caddy
  Basic Auth + TLS (see the hosted section below).
- **"Coming Soon" is enforced server-side** — activation, capital allocation, and backtest are
  rejected with `409` for not-ready strategies. The UI hiding is cosmetic only.
- **Orders go to Alpaca paper only** — `ENABLE_LIVE_ORDERS` OFF (default) = simulated fills,
  no order sent; ON = tagged orders to `https://paper-api.alpaca.markets`. Any other
  `APCA_API_BASE_URL` refuses every write. No code path sends orders to Tastytrade.
- **Kill switch is real** — cancels every tracked async worker (even if flatten throws), is
  idempotent, and reports partial failure (`fully_flat`). Flatten cancels and closes only
  the app's own (`tsys-`) orders and quantities on the shared Alpaca account.

## Run locally (no Docker)

Backend (boots with zero infra — SQLite). Put the Alpaca paper keys in `backend/.env`
(`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`; `APCA_API_BASE_URL` defaults to
`https://paper-api.alpaca.markets`) for orders, and the Tastytrade OAuth provider secret +
refresh token for market data; the API tab shows both connections:

```bash
cd backend
../.venv/bin/pip install -r requirements.txt
../.venv/bin/python -m uvicorn app.main:app --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev   # operator console at http://localhost:5173  (talks to backend on :8000)
npm run build # production build to frontend/dist (served by run_web.sh and Docker)
```

## Run as a web app (browser)

One command builds the React UI and serves it together with the API from a single
uvicorn process, then opens it in your browser — no native window, no separate dev server:

```bash
./scripts/run_web.sh          # serves http://127.0.0.1:8765 and opens it
```

It binds to `127.0.0.1` (local machine only). For LAN access change `--host` to `0.0.0.0`
in the script —
**local network only; do not expose to the internet without adding real auth + TLS** (see
`docs/superpowers/specs/2026-06-26-web-app-migration-design.md`).

## Run as a background service (macOS LaunchAgent)

`com.tradingsystem.web` runs the same server as `run_web.sh` (API + built UI on
`http://127.0.0.1:8765`) at login, and restarts it if it exits:

```bash
./scripts/install_autostart.sh     # write + load ~/Library/LaunchAgents/com.tradingsystem.web.plist
./scripts/uninstall_autostart.sh   # stop + remove it
launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web   # restart after code changes
```

To open it from a phone or another device on the same Wi-Fi, install it in LAN mode and
browse to the Mac's address (e.g. `http://<mac>.local:8765`):

```bash
RUN_WEB_HOST=0.0.0.0 ./scripts/install_autostart.sh
```

LAN mode is plain HTTP and the dashboard's read views have no login, so anyone on the
network can see account and position data. Gated actions (activate, kill, order routing)
still need `APP_AUTH_TOKEN`, which each device stores separately (paste it in Connections).
Never port-forward this to the internet; use the Docker stack below for remote access.
Re-running the install script without `RUN_WEB_HOST` switches back to this Mac only.

Logs go to `~/Library/Logs/trading-system.{out,err}.log`. The plist doesn't set
`ENABLE_LIVE_ORDERS`; order routing follows `backend/.env` only. Re-run the install script
after moving the repo. Rebuild the UI (`cd frontend && npm run build`) before restarting so
the service serves your frontend changes.

## Strategies

**QQQ First-Hour Credit Spread** (`qqq_credit_spread`) runs **in this app on the Alpaca
paper account**; Tastytrade supplies market data only. Code:
`backend/app/strategies/qqq_credit_spread/`. Its entry and close rules are a port of the
external runner (`~/live_system_maintenance/live`, `com.karan.spreadlive`), checked for
parity against that runner's own functions (`backend/tests/test_qqq_spread_logic.py`).
Unlike the runner it has **no ML model**: every spread is held to the 15:50 flat exit.

- **10:30 ET entry.** Reads the completed first hour (Tastytrade 1-minute candles): green
  sells a $1 put spread, red a call spread, flat skips. The limit credit is the live option
  mid, re-priced every 15 s (cancel-confirmed, then a fresh tagged order); unfilled at
  10:40 it is cancelled.
- **15:50 flat exit.** No model, stop or target: the whole spread closes at 15:50, walking
  the limit from mid to marketable, aiming to be flat by 15:55 (half days: 12:50 / 12:55).
- **Sizing:** one spread per $1,000 allocated (the first at $500), capped at
  `QQQ_SPREAD_MAX_CONTRACTS` (1 while testing). No allocation, no trade.
- **Safety:** every order goes through the app's tagged, paper-locked Alpaca path, and every
  open/closed decision reads what the app's own tagged orders filled. The day's session row
  is written before any order, so a restart never enters twice; a restart mid-entry cancels
  the resting order and trusts the broker. Stopping the worker cancels its working order.
- **Activation is refused** while `ENABLE_LIVE_ORDERS` is off (it never runs on simulated
  fills), without Alpaca keys, or while `com.karan.spreadlive` runs with
  `--alpaca-paper`: that paper copy uses the same Alpaca paper account and closes by the
  positions Alpaca reports. The daemon may keep trading real money on Tastytrade.
- Sessions: `GET /api/strategies/qqq_credit_spread/sessions`. Settings: `QQQ_SPREAD_*` in
  `backend/app/config.py` (grace, re-price cadence, close times, sizing).

**XSP 0DTE Iron Fly** (`xsp_iron_fly`) trades **on the Alpaca paper account** from this app,
with real orders and fills; there is no simulated mode. It sells a 10-point-wide XSP iron fly
at 09:45 ET, gives each short leg its own 1.5x stop, and holds the rest to the 4:00 pm cash
settlement. Quotes for decisions come from Tastytrade, which never receives orders. Code:
`backend/app/strategies/xsp_iron_fly/`.

- **Orders.** Everything goes through the app's shared, tagged order path:
  `orders.submit_combo_order` for the 4-leg entry, `orders.submit_option_order` for single
  legs. Activation needs `ENABLE_LIVE_ORDERS` on, Alpaca keys, and the paper host.
  `XSP_FLY_MODE` defaults to `alpaca_paper`; `live` is refused.
  - **Entry** is one `mleg` DAY limit with a negative `limit_price`, which is the least
    credit accepted. A 2026-11-24 test order on this paper account confirmed that
    convention for XSP: Alpaca stored `-9.5`, and the order rested unfilled.
  - **Stops** are single-leg `buy_to_close` limits, on the option's price increment
    ($0.01 under $3, $0.05 from $3). Wings are never stopped. A rejected stop close
    alerts once and retries every tick. In SAFE_MODE, opening orders are blocked but
    closing orders still go out.
  - **Fills.** Index options don't accept IOC, so each order is polled for a few
    seconds, then cancelled and re-read. Fill prices are Alpaca's own per-leg averages,
    and each fill records its Alpaca order id.
  - **Fail-safe.** If an order can't be read, filled without prices, or is still working
    after its cancel, SAFE_MODE trips and the day is left to a human.
  - **Restart.** The app cancels orders a crash left working on the day's legs, then
    compares Alpaca's app-owned fills with the session. A mismatch trips SAFE_MODE. It
    never auto-applies unseen fills.
  - **End of day.** Reconciliation adds the same Alpaca check. Alpaca posts the cash
    settlement around 21:00 ET (`OPCSH`/`OPEXP`, visible the next day on paper).
  - **Mode guard.** A fly recorded under another mode (e.g. the old simulator) is never
    managed. It's left alone, with an alert.
- **Expired options in the ledger.** The app's Alpaca ledger, and the local side of
  startup reconciliation, drop **cash-settled index** option contracts (XSP, SPX/SPXW, NDX,
  RUT, VIX…) past their expiry: the session close on the expiry day, 13:00 ET on half
  days. A leg held to cash settlement has no closing order. Physically settled
  equity/ETF options (e.g. QQQ) are never dropped, because they turn into shares.
- **Sizing.** 1 fly per $5,000 of the strategy's own equity: `starting_equity` ($5,000)
  plus the realized P&L of all its finished sessions, net of fees. It never sizes below 1
  fly while equity still covers one fly's max loss. There is no contract cap. The
  Strategies-page capital allocation doesn't affect this strategy's size.
- **Settings** are `XSP_FLY_*` in `backend/.env` (see `config.py`). Defaults:
  `starting_equity=5000`, `capital_per_fly=5000`, `max_daily_loss_per_contract=1000`
  (the day's limit is this times the flies sized that day, and a fly whose max loss
  exceeds it isn't traded), `max_leg_spread=0.15`, `max_quote_age=5s` and
  `min_minutes_to_close=240`, which skips 13:00 half days.
- **State.** Each trading day gets one `xsp_fly_sessions` row, unique per day and written
  before any order, plus one `TradeRecord` per leg. A restart never enters the same day
  twice, and it picks up open legs with their stop levels. The emergency stop closes the
  leg records, treats the day as done, and leaves the strategy `stopped` so it doesn't
  auto-resume at boot. A plain **Stop** while a fly is open leaves its legs open and
  unmanaged until you activate it again (same day: monitoring resumes; later: the legs are
  settled at that day's close).
- **Daily-loss breach.** A breach sets a persisted halt that blocks later entries until it's
  cleared with `POST /api/strategies/xsp_iron_fly/clear-halt`.
  `GET /api/strategies/xsp_iron_fly/sessions` shows recent days: legs, stops, P&L and the
  end-of-day reconciliation, including the check against Alpaca's fills.

## Run the full stack (Docker, hosted)

Postgres + FastAPI (serves the UI same-origin) + Caddy (TLS + Basic Auth). One image
builds the React UI and serves it with the API — no separate frontend container.

Docker uses the two (git-ignored) `.env` files directly — there are no example templates:

- **`./.env`** (repo root) — stack settings read by `docker-compose.yml`:
  `SITE_ADDRESS` (e.g. `localhost`), `BASIC_AUTH_USER`, `BASIC_AUTH_HASH`
  (`docker run --rm caddy:2-alpine caddy hash-password --plaintext '<password>'`),
  `APP_SECRET_KEY`, and optionally `POSTGRES_PASSWORD` (defaults to `trading`).
- **`backend/.env`** — app settings: Alpaca paper keys (`APCA_API_KEY_ID`,
  `APCA_API_SECRET_KEY`), Tastytrade creds (`TT_PROVIDER_SECRET`, `TT_REFRESH_TOKEN`,
  `TT_ACCOUNT_NUMBER`), `APP_AUTH_TOKEN`, `ENABLE_LIVE_ORDERS`. It is mounted into the
  backend container (`/app/.env`) read-write, so settings saved from the API tab persist.
  It must exist before `docker compose up` (otherwise Docker creates a directory there).

```bash
docker compose up --build
# open https://localhost          (self-signed cert by default; accept it)
```

The API tab shows the Tastytrade (data) and Alpaca paper (orders) connections once the
stack is up — no separate gateway process to run.

**Security:** the app has no per-user login (auth == "does the Tastytrade OAuth probe
verify?"), so the Caddy **Basic Auth + TLS** layer is the only thing protecting order
routing. The backend
port is never published to the host. Do not bypass the proxy or expose the backend directly.
Set a strong `BASIC_AUTH_HASH` (`caddy hash-password`) and a real `APP_SECRET_KEY`. See
`docs/superpowers/specs/2026-06-26-web-app-migration-design.md`.

## Tests

```bash
cd backend
../.venv/bin/python -m tests.test_kill_switch
../.venv/bin/python -m tests.test_backtest
DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_journal
DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_strategies
DATABASE_URL=sqlite+aiosqlite:///./_t.db ../.venv/bin/python -m tests.test_engine_worker
```

Frontend (options builder payoff and tracked-trade math):

```bash
cd frontend
node --test src/components/builder/*.test.mjs
```

## Adding real strategy logic

Implement a `BaseStrategy` subclass with `ready = True`, the overview metadata, and `scan()`
returning ranked `Candidate`s; swap it into `backend/app/strategies/registry.py`. The engine,
API gating, and backtest pick it up automatically.
