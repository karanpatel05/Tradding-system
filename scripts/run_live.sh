#!/usr/bin/env bash
# LaunchAgent entry point: ALWAYS serve the `main` branch, whatever branch the dev
# checkout happens to be on.
#
# Keeps a dedicated, service-owned git worktree (detached at `main`) next to the repo,
# refreshes it to the latest local `main` on every (re)start, rebuilds the UI when the
# commit changed, and runs it against the repo's real data: backend/.env (symlinked, so
# API-tab writes still land in the repo's file), trading.db, data_cache and the session
# store. Restart the agent after merging to main to pick it up:
#   launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
#
# Installed (copied outside the repo) by scripts/install_autostart.sh, so it works no
# matter which branch the repo is on. Env: TS_REPO (required), TS_LIVE, TS_BRANCH,
# RUN_WEB_HOST, RUN_WEB_OPEN.
#
# The live worktree is NOT for editing: local changes there are discarded on start.
set -euo pipefail

REPO="${TS_REPO:?TS_REPO must point at the repo checkout}"
LIVE="${TS_LIVE:-$REPO-live}"
BRANCH="${TS_BRANCH:-main}"
log() { echo "[run_live $(date '+%F %T')] $*"; }

if [[ ! -e "$LIVE/.git" ]]; then
  log "creating live worktree $LIVE (detached at $BRANCH)"
  git -C "$REPO" worktree add --detach "$LIVE" "$BRANCH"
fi

# Pin to the latest local $BRANCH. --force: this tree is service-owned.
git -C "$LIVE" checkout --quiet --force --detach "$BRANCH"
git -C "$LIVE" clean -fdq          # untracked, non-ignored leftovers only
SHA="$(git -C "$LIVE" rev-parse HEAD)"
log "serving $BRANCH @ ${SHA:0:7}"

# Shared, git-ignored runtime pieces live in the repo.
ln -sfn "$REPO/backend/.env" "$LIVE/backend/.env"
ln -sfn "$REPO/.venv" "$LIVE/.venv"

# Frontend: reinstall deps when the lockfile changes; rebuild when the commit changes.
FE="$LIVE/frontend"
LOCK_SHA="$(shasum "$FE/package-lock.json" | cut -d' ' -f1)"
if [[ ! -d "$FE/node_modules" || "$(cat "$FE/node_modules/.lock-sha" 2>/dev/null)" != "$LOCK_SHA" ]]; then
  log "installing frontend dependencies"
  ( cd "$FE" && npm ci --no-audit --no-fund ) && echo "$LOCK_SHA" > "$FE/node_modules/.lock-sha"
fi
if [[ "$(cat "$FE/dist/.built-from" 2>/dev/null)" != "$SHA" ]]; then
  log "building frontend for ${SHA:0:7}"
  if ( cd "$FE" && VITE_API_BASE="" npm run build ); then
    echo "$SHA" > "$FE/dist/.built-from"
  elif [[ -d "$FE/dist" ]]; then
    log "WARNING: frontend build failed; serving the previous build"
  else
    log "ERROR: frontend build failed and no previous build exists"; exit 1
  fi
fi

export DATABASE_URL="sqlite+aiosqlite:///$REPO/backend/trading.db"
export HISTORY_CACHE_DIR="$REPO/backend/data_cache"
export SESSION_STORE_PATH="$REPO/backend/.session_store.json"
exec /bin/bash "$LIVE/scripts/run_web.sh"
