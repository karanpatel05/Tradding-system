# Multi-Strategy Automated Trading System (Tastytrade)

Production-grade scaffold for running up to 5 trading strategies concurrently against
the Tastytrade API. Strategy logic is added later — the engine, auth, dashboard, journal,
and backtest framework are built so real rules drop in without rewriting anything.

## Architecture

```
backend/   FastAPI + async SQLAlchemy + tastytrade SDK (REST + DXLink)
  app/
    config.py            env-driven settings (provider_secret optional at boot)
    auth/                OAuth session manager (server-side), app gate, routes
    tastytrade/          SDK wrapper: OAuth, accounts, positions, quotes, candles
    engine/              kill-switch task registry, per-strategy workers, flatten stub
    strategies/          BaseStrategy + registry of Strategy 1–5 ("Coming Soon")
    backtest/            pure-Python metrics + historical runner (max DXFeed history)
    services/            market status/overview, logging + journal/calendar
    routers/             dashboard, strategies, journal, websocket
    db/                  models + async engine (SQLite dev / Postgres prod)
  tests/                 kill switch, journal, strategy gating, worker, backtest
frontend/  React + Vite + Recharts (auth gate, dashboard, strategy tab, calendar)
docker-compose.yml       postgres + backend + frontend
```

## Key guarantees

- **Auth gates the whole app** — every REST route and the WebSocket push channel check the
  session cookie. The Tastytrade refresh/session tokens stay server-side; the browser only
  holds an opaque HttpOnly cookie. The OAuth client secret can be set server-side
  (`TASTYTRADE_PROVIDER_SECRET`) **or** entered in the auth panel — either way it's used only
  on the server and never returned to the browser.
- **"Coming Soon" is enforced server-side** — activation, capital allocation, and backtest are
  rejected with `409` for not-ready strategies. The UI hiding is cosmetic only.
- **Kill switch is real** — cancels every tracked async worker (even if flatten throws), is
  idempotent, and reports partial failure (`fully_flat`). Position flattening is a labelled stub
  until order routing is enabled.

## Run locally (no Docker)

Backend (boots with zero infra — SQLite + auth panel shows even without OAuth configured):

```bash
cd backend
../.venv/bin/pip install -r requirements.txt
# optional: export TASTYTRADE_PROVIDER_SECRET=... to enable real login
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

Same backend as the Mac desktop app (`desktop/`), just a different run mode. It binds to
`127.0.0.1` (local machine only). For LAN access change `--host` to `0.0.0.0` in the script —
**local network only; do not expose to the internet without adding real auth + TLS** (see
`docs/superpowers/specs/2026-06-26-web-app-migration-design.md`).

## Run the full stack (Docker)

```bash
export TASTYTRADE_PROVIDER_SECRET=your_oauth_client_secret
docker compose up --build
# open http://localhost:5173
```

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