#!/usr/bin/env bash
# Install a macOS LaunchAgent (com.tradingsystem.web) that runs this project's backend
# server — API + built UI on http://127.0.0.1:8765 — on login (quietly, no browser) and
# restarts it if it exits. Re-running it replaces the agent (e.g. after moving the repo).
#
#   ./scripts/install_autostart.sh      # generate + load the LaunchAgent
#   ./scripts/uninstall_autostart.sh    # stop + remove it
#
# Order routing is NOT armed here: ENABLE_LIVE_ORDERS comes only from backend/.env
# (default false = simulated fills). An env var in this plist would override that file,
# so don't add one while the project is in development.
#
# NOTE: don't also run `./scripts/run_web.sh` manually while the agent is active —
# they'd both want port 8765. The agent becomes the single managing process.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.tradingsystem.web"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs"
GUI="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ROOT/scripts/run_web.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>RUN_WEB_OPEN</key><string>0</string>
    <key>RUN_WEB_HOST</key><string>127.0.0.1</string>
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
echo "    Logs: $LOGDIR/trading-system.{out,err}.log"
echo "    Status: launchctl print $GUI/$LABEL | head"
echo "    Server: http://127.0.0.1:8765  (give it a few seconds to boot)"
