#!/usr/bin/env bash
# Run the trading system as a plain web app: uvicorn serves the API + the built
# React UI same-origin on 127.0.0.1:8765; open it in any browser. (The Mac desktop
# app remains a separate, parallel run mode — same backend.)
#
#   ./scripts/run_web.sh                 # build UI if needed, then serve + open browser
#   FRONTEND_BUILD=1 ./scripts/run_web.sh  # force a fresh frontend build
#   (edit --host to 0.0.0.0 for LAN access — local network only; see security note in
#    docs/superpowers/specs/2026-06-26-web-app-migration-design.md)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_BUILD="${FRONTEND_BUILD:-}"

if [[ -n "$FRONTEND_BUILD" || ! -d "$ROOT/frontend/dist" ]]; then
  echo "==> Building frontend..."
  ( cd "$ROOT/frontend" && VITE_API_BASE="" npm run build )
fi

URL="http://127.0.0.1:8765"
echo "==> Starting web app at $URL  (Ctrl-C to stop)"
# Open the browser once the server is up (best-effort, backgrounded). Set RUN_WEB_OPEN=0
# to run quietly without opening a browser (the auto-start LaunchAgent uses this).
if [[ "${RUN_WEB_OPEN:-1}" != "0" ]]; then
  ( for _ in $(seq 1 30); do curl -s -m1 "$URL/health" >/dev/null 2>&1 && break; sleep 1; done
    command -v open >/dev/null 2>&1 && open "$URL" ) &
fi

# IBKR Gateway runs on the LOCAL host for a web/dev run. Default to 127.0.0.1 so a
# repo-root `.env` written for Docker (IBKR_HOST=host.docker.internal) doesn't shadow
# local runs — uvicorn loads that root .env since it starts from the repo root. Still
# overridable: `IBKR_HOST=... ./scripts/run_web.sh`.
FRONTEND_DIST="$ROOT/frontend/dist" \
  DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///$ROOT/backend/trading.db}" \
  HISTORY_CACHE_DIR="${HISTORY_CACHE_DIR:-$ROOT/backend/data_cache}" \
  IBKR_HOST="${IBKR_HOST:-127.0.0.1}" \
  IBKR_PORT="${IBKR_PORT:-4002}" \
  IBKR_CLIENT_ID="${IBKR_CLIENT_ID:-3}" \
  "$ROOT/.venv/bin/uvicorn" app.main:app \
    --host "${RUN_WEB_HOST:-127.0.0.1}" \
    --port 8765 \
    --app-dir "$ROOT/backend"
