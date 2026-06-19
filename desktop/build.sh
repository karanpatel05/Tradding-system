#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin"

echo "==> Building frontend (same-origin)"
( cd "$ROOT/frontend" && VITE_API_BASE="" npm run build )

echo "==> Bundling with PyInstaller"
( cd "$ROOT/desktop" && "$PY/pyinstaller" --noconfirm --clean TradingSystem.spec )

echo "==> Done: $ROOT/desktop/dist/TradingSystem.app"
