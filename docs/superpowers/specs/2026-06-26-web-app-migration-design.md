# Web App Migration Design

Date: 2026-06-26
Status: Scoping / pre-implementation design.
Author: Claude agent (design only — no application code was modified).

---

## 0. Honest framing

**This is already a web app.** The core is FastAPI over HTTP — `backend/app/main.py`, WebSocket
in `routers/ws.py`, REST API under `/api/*`. The Mac desktop app (`desktop/app.py`) is simply a
thin launcher that starts uvicorn in a daemon thread and opens a PyWebView window pointed at
`127.0.0.1:8765`. The back-end does not know or care that the client is a native window vs a
real browser. `backend/app/static.py` and the `FRONTEND_DIST` env var already provide same-origin
SPA serving. `frontend/src/api.js:wsUrl()` already derives the WebSocket URL from `window.location`
when `API_BASE` is empty.

The migration question is therefore not "rewrite the app to be a web app." It is:

> Pick a **run mode** (local browser / hosted), and understand the security and operational
> implications of that choice.

---

## 1. Architecture as confirmed (corrections to the task brief)

| What the task said | What the code actually shows |
|---|---|
| Auth is "connection-state only" | Correct. `require_auth` → `session_manager.is_authenticated()` → `gateway.is_connected()`. There is **no per-user identity, no login, no session cookie in active use.** The cookie constant (`ts_session`) and `APP_SESSION_COOKIE` in `gate.py` are vestigial and explicitly noted "no longer used to gate." The `app_secret_key` setting exists but nothing signs or validates a cookie today. |
| No user login / no real access control | Correct. Protected routes check "is the IBKR Gateway currently connected." Any caller that hits a protected endpoint while the Gateway is up is considered authenticated — including a remote attacker. |
| `static.py` mounts SPA when `FRONTEND_DIST` is non-empty | Correct. The mount is registered last, after all `/api/*` and `/ws` routes, with proper SPA fallback. The `assets/` directory gets a separate `StaticFiles` mount that returns 404 (not the SPA) for cache misses — correct behaviour. |
| `api.js` uses `import.meta.env.DEV` guard | Correct. `API_BASE` is `""` in a production build; `wsUrl()` derives `ws://` or `wss://` from `window.location`. Same-origin is fully functional for both REST and WebSocket. |
| A Docker/hosted path needs building from scratch | Partially wrong. `docker-compose.yml` and `backend/Dockerfile` already exist but are **stale**: they reference `TASTYTRADE_PROVIDER_SECRET`, serve the frontend via a separate nginx container with `VITE_API_BASE: http://localhost:8000` (cross-origin model, not same-origin), and predate the IBKR migration. The README is likewise stale (still references Tastytrade OAuth). These are assets to repair, not create. |
| IBKR Gateway runs at `127.0.0.1:4002` | Correct. `ibkr/gateway.py` reads host/port from settings (`ibkr_host`, `ibkr_port`). The Gateway is a separate GUI Java application (IB's own software) that must run on the same machine as the FastAPI process. **This is the binding constraint for any hosted scenario.** |

**Key implication of the auth finding:** for any option where the app is reachable over a network, you are building user authentication from zero — not hardening an existing one. There is nothing to harden.

---

## 2. The three options

### Option A — Local browser app (recommended for personal use)

**What it is:** Run uvicorn serving the built frontend on `127.0.0.1`, open the URL in a
regular browser. The native window (PyWebView) is not used. The Mac desktop `.app` continues to
exist alongside this — it is just another run mode over the same backend.

**What changes:**
- A new shell script `scripts/run_web.sh` (or `Makefile` target). No application code changes.
- A one-time `npm run build` with `VITE_API_BASE=""` to produce `frontend/dist/`.

**How to run (manual steps, pre-script):**
```bash
# 1. Build the frontend (once; or after any UI change)
cd /path/to/repo/frontend
VITE_API_BASE="" npm run build      # outputs frontend/dist/

# 2. Start the backend, serving the built UI
cd /path/to/repo/backend
FRONTEND_DIST=../frontend/dist \
  DATABASE_URL=sqlite+aiosqlite:///./trading.db \
  HISTORY_CACHE_DIR=./data_cache \
  ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765

# 3. Open http://127.0.0.1:8765 in any browser
```

**Proposed script — `scripts/run_web.sh`:**
```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_BUILD="${FRONTEND_BUILD:-}"

if [[ -n "$FRONTEND_BUILD" || ! -d "$ROOT/frontend/dist" ]]; then
  echo "==> Building frontend..."
  ( cd "$ROOT/frontend" && VITE_API_BASE="" npm run build )
fi

echo "==> Starting backend at http://127.0.0.1:8765"
FRONTEND_DIST="$ROOT/frontend/dist" \
  DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///$ROOT/backend/trading.db}" \
  HISTORY_CACHE_DIR="${HISTORY_CACHE_DIR:-$ROOT/backend/data_cache}" \
  "$ROOT/.venv/bin/uvicorn" app.main:app \
    --host 127.0.0.1 \
    --port 8765 \
    --app-dir "$ROOT/backend"
```

**What does NOT need to change (important — these are already correct):**
- `backend/app/static.py` — already works for any run mode that sets `FRONTEND_DIST`.
- `backend/app/main.py` — already calls `mount_frontend`.
- `backend/app/config.py` — `frontend_dist` is already env-driven; `cors_origins` is env-driven.
- `frontend/src/api.js` — `wsUrl()` already handles same-origin.
- `frontend/vite.config.js` — no changes needed; `VITE_API_BASE=""` is passed at build time.
- All in-flux files (`execution.py`, `registry.py`, `config.py`, `worker.py`, `db/models.py`) — **zero touches needed for Option A.** All configuration is via environment variables.

**CORS note for local browser:** `cors_origins` defaults to `http://localhost:5173,http://127.0.0.1:5173` (the Vite dev server). For local browser mode the frontend is same-origin (served by uvicorn on port 8765), so CORS middleware is never invoked for browser requests. No config change needed.

**What to do with `desktop/`:** Retain as-is. Option A and the desktop app are parallel run modes over the same backend. The `.app` bundle can still be built with `desktop/build.sh`. There is no conflict.

**Security posture:** Localhost-only (`--host 127.0.0.1`). No network exposure beyond the local machine. Same threat model as the desktop app — effectively single-user, no auth needed beyond locking the screen.

**Effort:** ~30 minutes (write the script, do a build, test). No code changes to the application itself.

---

### Option B — Hosted / remote web app

**What it is:** The backend runs on a machine (personal server, NUC, cloud VM) that also has IB
Gateway installed and logged in. A reverse proxy (nginx or Caddy) provides TLS and forwards to
uvicorn. The UI is reachable from any browser at `https://your-host/`.

**The binding constraint — IB Gateway co-location:** IB Gateway is a desktop GUI Java application.
It cannot be containerized. It must run on the same host as the FastAPI backend (it binds to
`127.0.0.1:4002`). This means "remote/hosted" is not "deploy to a PaaS or cloud function" —
it is "run on a specific server machine that also has IB Gateway". If you use a cloud VM, IB
Gateway must be installed there (possible with VNC/virtual display), which adds operational
complexity. A NUC or Mac mini at home with port-forwarding is the more common personal-trading
setup for this reason.

**What "hosted" adds / changes:**

| Concern | What must be done |
|---|---|
| **User authentication** | Build from zero — there is none today. The safest minimal approach is HTTP Basic Auth at the reverse proxy level (nginx `htpasswd`). A proper approach adds a login page + server-side session (FastAPI-Users or similar). Either way, every protected route must check identity, not just "is the Gateway connected." |
| **Transport security (TLS)** | Required. Use Caddy (auto HTTPS via Let's Encrypt) or nginx with Certbot. Never expose uvicorn directly on port 80/443. |
| **CORS hardening** | `CORS_ORIGINS` must list only your actual origin (`https://your-host`). The default `allow_credentials=True` is fine once origins are exact — but it is dangerous if origins is `*`. Verify `cors_origins` in `.env`. |
| **`app_secret_key`** | The default is `dev-insecure-change-me`. If session cookies are ever reintroduced, this must be a strong random secret set via env. Even today it should be overridden as hygiene. |
| **Bind address** | uvicorn must bind to `127.0.0.1`, not `0.0.0.0`. The reverse proxy is the only internet-facing listener. |
| **Kill switch / live orders exposure** | Any authenticated user on the remote interface can POST `/api/kill` (kill switch) or trigger live order submission. If this is a personal tool for one person, that person is the only user. If multiple users are ever contemplated, per-route RBAC is needed. |
| **WebSocket** | The reverse proxy must forward WebSocket upgrade headers (`Upgrade`, `Connection`). Both Caddy and nginx support this; nginx needs explicit `proxy_pass` + upgrade headers. |
| **Session persistence** | `session_store_path` writes to disk. On a server this is fine; ensure the file is readable only by the process user (`chmod 600`). |

**File-level changes for Option B (beyond Option A):**

New files (safe to create now or after Phase 2):
- `scripts/run_web.sh` — same as Option A but bind `127.0.0.1`, intended to run behind proxy.
- `deploy/nginx.conf` or `deploy/Caddyfile` — reverse proxy config with TLS, WebSocket forwarding.
- `deploy/systemd/tradingsystem.service` — if running as a systemd unit on Linux.
- `deploy/.env.example` — documents all required env vars for a production deployment.

Files to edit (mark "after Phase 2" due to concurrent flux):
- `backend/app/config.py` — add any new auth-related settings (e.g., `admin_password_hash`). **Coordinate with Phase-2 author; inject via env vars until Phase 2 merges.**
- `docker-compose.yml` — modernize to IBKR model (remove Tastytrade references, adopt same-origin serving pattern so frontend is built into the backend image, not a separate nginx container). This is safe to update independently of Phase 2 as it doesn't touch application code.

Files that do NOT need to change:
- All application Python files including `config.py` (settings are env-driven, can be overridden without code changes).
- `frontend/src/api.js` — `wsUrl()` already handles `wss://` when the page is served over HTTPS.
- `backend/app/static.py`, `backend/app/main.py` — already correct.

**Stale docker-compose.yml — repair plan (safe to do independently):**
The existing compose file is broken for the current codebase (Tastytrade refs, cross-origin frontend). Repairs needed:
1. Remove `TASTYTRADE_PROVIDER_SECRET`, `TASTYTRADE_IS_TEST`.
2. Add `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID` environment vars.
3. Replace the nginx frontend service with a frontend build stage that outputs into the backend image (same-origin model), or keep a separate nginx that acts as reverse proxy only.
4. Add `FRONTEND_DIST` and `CORS_ORIGINS` env vars in the backend service.
5. Note: IB Gateway cannot be in a compose service — document it as a prerequisite.

**Effort:** Several days. Auth scaffolding alone (even minimal Basic Auth at proxy level) is non-trivial to set up securely and test.

---

### Option C — Keep both desktop + web simultaneously

**What it is:** Retain the desktop build as the primary daily driver; add Option A's run script
so the same backend can also be used from a browser (e.g., from another machine on the LAN).

**Assessment:** This is already the natural state. The desktop app and the web run mode are just
two entry points over the same backend. There is no conflict. No "keeping both" work is needed —
it already works. The `desktop/build.sh` flow and the `scripts/run_web.sh` flow are independent.

If you want LAN access (another device on the same network), change `--host 127.0.0.1` to
`--host 0.0.0.0` in the run script. This is LAN-local but not internet-exposed. The absence of
auth is acceptable within a private home network but is a conscious decision.

---

## 3. Security verdict

**Current state (desktop app / localhost):** The absence of user auth is intentional and safe.
The browser's same-origin policy, the `127.0.0.1` bind, and the physical machine's access control
are the effective security perimeter. This is a common and reasonable posture for a personal
local tool.

**If you make the app reachable over the internet (Option B), the current auth model is
dangerous.** Specifically:

1. `require_auth` checks whether the IBKR Gateway is connected process-wide. While the Gateway
   is connected (which it will be during trading hours), **every HTTP request from every IP
   address passes auth.** Anyone who can reach the server can place orders, hit the kill switch,
   start/stop strategies, and read all trade history.

2. The `app_secret_key` default (`dev-insecure-change-me`) is a known string in the codebase.
   Even if session cookies were ever reinstated, this default makes them trivially forgeable.

3. The WebSocket endpoint at `/ws` is gated by `is_authenticated()` — same problem. Any caller
   while the Gateway is up receives the live market data / trade push stream.

**Minimum viable security for Option B (in priority order):**
1. Reverse proxy with TLS (Caddy or nginx + Certbot). Non-negotiable.
2. HTTP Basic Auth at the proxy layer — even a single-user `htpasswd` entry blocks unauthenticated
   access completely. This is the fastest path to a safe hosted setup.
3. Override `APP_SECRET_KEY` in production `.env` with `openssl rand -hex 32`.
4. Set `CORS_ORIGINS` in `.env` to exactly `https://your-host` (no wildcards).
5. Bind uvicorn to `127.0.0.1`, not `0.0.0.0`.
6. Eventually (if multi-user is ever needed): replace the proxy-level Basic Auth with a proper
   application-level login (FastAPI-Users + bcrypt + session cookie). This touches `auth/` and
   `config.py`; coordinate with Phase-2 if those files are in flux.

---

## 4. Phase-2 coordination

The following files are currently in flux due to concurrent options-trading work and must not
be modified as part of the web migration unless explicitly coordinated:

- `backend/app/engine/execution.py`
- `backend/app/strategies/registry.py`
- `backend/app/config.py`
- `backend/app/engine/worker.py`
- `backend/app/db/models.py`

**Option A requires zero edits to any of these files.** All configuration (frontend dist path,
data dir, CORS) is via environment variables that are already implemented.

**Option B may eventually want to add settings to `config.py`** (e.g., `admin_password_hash`,
`session_max_age`). These additions should be made after Phase-2 merges to main, or discussed
with the Phase-2 author first. The fastest path for Option B (reverse-proxy Basic Auth) needs **no** application-level setting at
all — the proxy handles it entirely — so it dodges `config.py` completely. If you ever need
app-level auth config (e.g., a hashed password field), add the field to `config.py` after Phase-2
merges, or read it directly from `os.environ` in a new auth module to keep `config.py` untouched
in the meantime.

---

## 5. Phased implementation plan

### Phase 1 — Local browser mode (Option A) — Recommended, low risk, ~30 min

1. `npm run build` (with `VITE_API_BASE=""`) to produce `frontend/dist/`.
2. Write `scripts/run_web.sh` (see script above).
3. Test: run the script, open `http://127.0.0.1:8765` in a browser, verify dashboard loads,
   WebSocket connects, IBKR connection works.
4. Update `README.md` with a "Run in browser" section pointing to the script.

New files:
- `scripts/run_web.sh` — the only new application-adjacent artifact.
- Optional: `Makefile` with `web` and `desktop` targets.

No changes to any Python application files. No changes to frontend source. The desktop `.app`
build process is unaffected.

### Phase 2 — Hosted mode (Option B) — After Phase-2 options work merges, if needed

Prerequisites (decisions the user must make first):
- Which host? (Mac mini, NUC, cloud VM)
- What domain/IP? (needed for TLS cert)
- Is IB Gateway running there, or will it stay on the laptop? (if laptop, B is not feasible)
- What level of auth? (reverse-proxy Basic Auth vs application-level login)

If proceeding:

1. Repair `docker-compose.yml`: remove Tastytrade refs, add IBKR env vars, switch to same-origin
   serving (build frontend into backend image), document IB Gateway as external prerequisite.
2. Write `deploy/nginx.conf` or `deploy/Caddyfile` with TLS, WebSocket proxy, Basic Auth.
3. Write `deploy/.env.example` documenting all required env vars.
4. Override `APP_SECRET_KEY` and `CORS_ORIGINS` in production `.env`.
5. Test from a remote browser: dashboard, WebSocket, auth rejection on bad credentials.
6. After Phase-2 merges: optionally add `admin_password_hash` setting to `config.py` for
   application-level auth.

New files in Phase 2:
- `deploy/nginx.conf` or `deploy/Caddyfile`
- `deploy/systemd/tradingsystem.service` (if Linux host)
- `deploy/.env.example`

Files that may be edited:
- `docker-compose.yml` — repair only, no application logic.
- `backend/app/config.py` — only after Phase-2 merges, and only for new auth settings.

---

## 6. File-level change summary

| File | Option A | Option B | Touches in-flux files? |
|---|---|---|---|
| `scripts/run_web.sh` | New (create) | New (create) | No |
| `Makefile` | Optional new | Optional new | No |
| `README.md` | Add "browser mode" section | Add "hosted" section | No |
| `docker-compose.yml` | No change | Repair (modernize) | No |
| `deploy/nginx.conf` or `Caddyfile` | N/A | New | No |
| `deploy/.env.example` | N/A | New | No |
| `backend/app/main.py` | No change | No change | N/A |
| `backend/app/static.py` | No change | No change | N/A |
| `backend/app/config.py` | No change | No change (env vars) until Phase-2 merges | Yes — defer |
| `frontend/src/api.js` | No change | No change | N/A |
| `frontend/vite.config.js` | No change | No change | N/A |
| `desktop/app.py` | No change | No change | N/A |
| `desktop/build.sh` | No change | No change | N/A |

---

## 7. Recommendation

**Implement Option A.** It is essentially already done — the infrastructure exists. The only work
is writing a run script and doing a frontend build. It takes ~30 minutes, touches zero application
files, does not conflict with Phase-2, and gives full browser access to the dashboard from the
local machine (and LAN if you change the bind address to `0.0.0.0`).

**Keep the desktop app.** It is a parallel run mode over the same backend. There is no reason to
retire it. Both can exist and be used interchangeably.

**Defer Option B** until Phase-2 merges and until the IB Gateway hosting question is resolved.
The biggest blocker is not code — it is the question of where IB Gateway runs. If it must stay on
your laptop, there is no practical hosted-backend story. If you have a dedicated server, the hosted
option is viable but requires real auth work.

**Do not expose the app to the internet without auth.** The current auth model (Gateway connected =
authenticated) makes every trading endpoint world-readable and world-writable when the Gateway is
up. The kill switch, order submission, strategy control — all unprotected. For a personal localhost
tool this is fine; for an internet-exposed tool it is not.
