# Multi-Strategy Automated Trading System (Alpaca paper orders, Tastytrade data)

Runs trading strategies with **orders routed to an Alpaca paper account** (paper-locked in
code; live Alpaca is a deliberate future code change) and **Tastytrade for market data
only** (quotes, option chains, history; OAuth personal grant). The Alpaca paper account
may be shared with other tools: the app only touches orders it tagged (`tsys-`
client_order_id). Includes a live engine, dashboard, journal, equity backtests and an
options Builder (paper-tracked, never places orders). Strategy slots are currently empty
(Coming Soon). Runs as a local web app or a hosted Docker stack.

## Architecture

```
backend/   FastAPI + async SQLAlchemy + httpx (Alpaca) + tastytrade SDK (data)
  app/
    config.py            env-driven settings (APCA_* Alpaca keys, Tastytrade OAuth creds)
    auth/                connection-state auth (authenticated == Tastytrade OAuth probe verifies)
    broker/              Alpaca paper orders (paper lock, tsys- tag), app-owned ledger, account
    engine/              kill-switch task registry, per-strategy workers, execution, recovery
    strategies/          BaseStrategy + registry (slots Coming Soon)
    backtest/            pure-Python metrics + historical runner
    services/            market status/overview, logging + journal/calendar
    routers/             dashboard, strategies, journal, websocket
    db/                  models + async engine (SQLite dev / Postgres prod)
  tests/                 kill switch, journal, strategy gating, worker, backtest
frontend/  React + Vite + Recharts (auth gate, dashboard, strategy tab, calendar)
docker-compose.yml       postgres + backend + frontend
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
npm run dev   # http://localhost:5173  (talks to backend on :8000)
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

## Run the full stack (Docker, hosted)

Postgres + FastAPI (serves the UI same-origin) + Caddy (TLS + Basic Auth). One image
builds the React UI and serves it with the API — no separate frontend container.

```bash
cp deploy/.env.example .env     # set SITE_ADDRESS, BASIC_AUTH_USER/HASH, APP_SECRET_KEY
docker compose up --build
# open https://localhost          (self-signed cert by default; accept it)
```

Set the Alpaca paper keys (`APCA_*`) and Tastytrade OAuth creds in the backend env; the API
tab shows both connections after the stack is up — no separate gateway process to run.

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

## Adding real strategy logic

Implement a `BaseStrategy` subclass with `ready = True`, the overview metadata, and `scan()`
returning ranked `Candidate`s; swap it into `backend/app/strategies/registry.py`. The engine,
API gating, and backtest pick it up automatical