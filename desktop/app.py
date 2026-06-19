"""TradingSystem desktop launcher: run the FastAPI backend in-process and show the
React dashboard in a native macOS window (PyWebView). Bundled into a .app by
PyInstaller (see TradingSystem.spec)."""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"


def resolve_data_dir() -> Path:
    """Writable per-user data dir (the .app bundle itself is read-only)."""
    d = Path(os.path.expanduser("~")) / "Library" / "Application Support" / "TradingSystem"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meipass() -> Path | None:
    mp = getattr(sys, "_MEIPASS", None)
    return Path(mp) if mp else None


def backend_path() -> Path:
    """Dir to add to sys.path so ``import app.main`` works. Frozen: the bundle root
    (the ``app`` package is bundled there). Dev: the repo's ``backend/``."""
    mp = _meipass()
    if mp and (mp / "app").exists():
        return mp
    return Path(__file__).resolve().parents[1] / "backend"


def bundled_dist() -> str:
    """Path to the built frontend. Frozen: ``frontend_dist`` in the bundle. Dev:
    the repo's ``frontend/dist`` (after an npm build) if present, else empty."""
    mp = _meipass()
    if mp and (mp / "frontend_dist").exists():
        return str(mp / "frontend_dist")
    dev = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    return str(dev) if dev.exists() else ""


def set_runtime_env(data_dir: Path, dist_dir: str) -> None:
    os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'trading.db'}")
    os.environ.setdefault("HISTORY_CACHE_DIR", str(data_dir / "data_cache"))
    if dist_dir:
        os.environ.setdefault("FRONTEND_DIST", dist_dir)


def wait_for_server(url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — server not up yet
            time.sleep(0.2)
    return False


def _serve() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")


def main() -> int:
    # Make the backend `app` package importable (dev: backend/, frozen: bundle root).
    sys.path.insert(0, str(backend_path()))

    set_runtime_env(resolve_data_dir(), bundled_dist())
    threading.Thread(target=_serve, daemon=True).start()
    if not wait_for_server(f"{URL}/health"):
        print("Backend failed to start", file=sys.stderr)
        return 1

    import webview  # lazy: keeps helpers importable without pywebview
    webview.create_window("Trading System", URL, width=1400, height=900)
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
