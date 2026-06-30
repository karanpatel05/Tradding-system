#!/usr/bin/env bash
# Stop + remove the trading-system auto-start LaunchAgent (installed by install_autostart.sh).
set -euo pipefail

LABEL="com.tradingsystem.web"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"

launchctl bootout "$GUI" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "==> Removed LaunchAgent '$LABEL'. The server is no longer auto-started on login."
echo "    (A currently-running instance may still be up until you stop it: lsof -ti tcp:8765 | xargs kill)"
