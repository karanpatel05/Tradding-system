#!/usr/bin/env bash
# Install a macOS LaunchAgent (com.tradingsystem.web) that runs this project's backend
# server — API + built UI on http://127.0.0.1:8765 — on login (quietly, no browser) and
# restarts it if it exits. Re-running it replaces the agent (e.g. after moving the repo).
#
# It ALWAYS serves GitHub's `main`: scripts/run_live.sh keeps a service-owned worktree
# (<repo>-live, detached at origin/main) and pulls it from GitHub on every start, so switching branches
# in the dev checkout never changes what the server runs. Data (.env, trading.db,
# data_cache) stays in the repo's backend/.
#
#   ./scripts/install_autostart.sh                   # keep the installed host (default 127.0.0.1)
#   RUN_WEB_HOST=0.0.0.0 ./scripts/install_autostart.sh   # also reachable on the LAN
#   ./scripts/uninstall_autostart.sh                 # stop + remove it
#
# LAN mode has no TLS and read routes have no login: anyone on your Wi-Fi can view the
# dashboard; order/kill/activate actions still need APP_AUTH_TOKEN. Never port-forward
# it to the internet (use the Docker stack's Caddy TLS + Basic Auth for that).
#
# Order routing is not set here: ENABLE_LIVE_ORDERS comes only from backend/.env
# (default true = Alpaca PAPER, and only with paper keys + the paper host). An env var in
# this plist would override that file, so don't add one.
#
# NOTE: don't also run `./scripts/run_web.sh` manually while the agent is active —
# they'd both want port 8765. The agent becomes the single managing process.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The main repo checkout (not a linked worktree), even when run from a worktree.
ROOT="$(cd "$(git -C "$HERE" rev-parse --git-common-dir)/.." && pwd)"
LABEL="com.tradingsystem.web"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs"
GUI="gui/$(id -u)"
# Keep the currently installed host (e.g. LAN 0.0.0.0) unless RUN_WEB_HOST is given.
CURRENT_HOST="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:RUN_WEB_HOST' "$PLIST" 2>/dev/null || true)"
HOST="${RUN_WEB_HOST:-${CURRENT_HOST:-127.0.0.1}}"
# The service always serves `main` from its own worktree (see scripts/run_live.sh).
# The launcher is copied outside the repo so it works whatever branch ROOT is on.
LIVE="$ROOT-live"
SUPPORT="$HOME/Library/Application Support/TradingSystem"
LAUNCHER="$SUPPORT/run_live.sh"

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR" "$SUPPORT"
install -m 0755 "$HERE/run_live.sh" "$LAUNCHER"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$LAUNCHER</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>RUN_WEB_OPEN</key><string>0</string>
    <key>RUN_WEB_HOST</key><string>$HOST</string>
    <key>TS_REPO</key><string>$ROOT</string>
    <key>TS_LIVE</key><string>$LIVE</string>
    <key>TS_BRANCH</key><string>main</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOGDIR/trading-system.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/trading-system.err.log</string>
</dict>
</plist>
PLIST

echo "==> Wrote $PLIST"

# Reload cleanly: bootout if already loaded (ignore errors), then bootstrap.
launchctl bootout "$GUI" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$GUI" "$PLIST"
launchctl enable "$GUI/$LABEL" 2>/dev/null || true

echo "==> Loaded LaunchAgent '$LABEL' (starts on login, restarts on exit)."
echo "    Serves GitHub's 'main' (origin/main, pulled on every restart) from $LIVE."
echo "    After merging on GitHub: launchctl kickstart -k $GUI/$LABEL"
echo "    Logs: $LOGDIR/trading-system.{out,err}.log"
echo "    Status: launchctl print $GUI/$LABEL | head"
echo "    Server: http://127.0.0.1:8765  (give it a few seconds to boot)"
if [[ "$HOST" == "0.0.0.0" ]]; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  echo "    LAN:    http://${LAN_IP:-<this-mac-ip>}:8765  or  http://$(scutil --get LocalHostName).local:8765"
fi
