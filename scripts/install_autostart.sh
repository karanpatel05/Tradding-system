#!/usr/bin/env bash
# Install a macOS LaunchAgent that auto-starts the trading-system web server on login
# (quietly, no browser) and restarts it if it exits. Connects to the local IB Gateway;
# with the always-connect behavior it keeps retrying until the Gateway is up.
#
#   ./scripts/install_autostart.sh      # generate + load the LaunchAgent
#   ./scripts/uninstall_autostart.sh    # stop + remove it
#
# NOTE: don't also run `./scripts/run_web.sh` manually while the agent is active —
# they'd both want port 8765. The agent becomes the single managing process. The IB
# Gateway (a GUI app) still needs to be running/logged-in for live data + trading.
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
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>RUN_WEB_OPEN</key><string>0</string>
    <key>IBKR_HOST</key><string>127.0.0.1</string>
    <key>IBKR_PORT</key><string>4002</string>
    <key>IBKR_CLIENT_ID</key><string>3</string>
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
