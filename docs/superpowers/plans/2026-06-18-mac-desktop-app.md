# Mac Desktop App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the FastAPI backend + React frontend as one double-clickable, unsigned `TradingSystem.app` for macOS using PyWebView + PyInstaller.

**Architecture:** A `desktop/app.py` launcher sets a writable data dir (Application Support) via env, runs uvicorn (`app.main:app`) in a daemon thread on `127.0.0.1:8765`, and opens a native PyWebView window at that URL. FastAPI serves the built React `dist/` same-origin (SPA fallback) when `FRONTEND_DIST` is set, so one process serves API + UI. PyInstaller bundles it into a `.app`.

**Tech Stack:** Python 3.14, FastAPI/uvicorn, pywebview, pyinstaller, React/Vite.

**Spec:** `docs/superpowers/specs/2026-06-18-mac-desktop-app-design.md`

> Run all `pytest` from `backend/`. Python: `/Users/karanpatel/PycharmProjects/Tradding-system/.venv/bin/python`. Repo is under git; commit messages end with the Co-Authored-By trailer.

---

### Task 1: Desktop build dependencies

**Files:**
- Create: `desktop/requirements-desktop.txt`

- [ ] **Step 1: Install pywebview + pyinstaller into the venv**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system && .venv/bin/pip install pywebview pyinstaller`
Expected: both install (pywebview pulls `pyobjc-*` on macOS for the Cocoa WKWebView).

- [ ] **Step 2: Verify pywebview imports**

Run: `.venv/bin/python -c "import webview; print('pywebview', webview.__version__)"`
Expected: prints a version (no error).

- [ ] **Step 3: Record build-only deps**

Create `desktop/requirements-desktop.txt`:
```
# Desktop packaging (build/run-time only; not needed for the web app)
pywebview>=5.0
pyinstaller>=6.0
```

- [ ] **Step 4: Commit**

```bash
cd /Users/karanpatel/PycharmProjects/Tradding-system
git add desktop/requirements-desktop.txt
git commit -m "build(desktop): add pywebview + pyinstaller build deps

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Config — `frontend_dist` setting

**Files:**
- Modify: `backend/app/config.py` (add one field near `history_cache_dir`)
- Test: `backend/tests/test_config_frontend_dist.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_config_frontend_dist.py
from app.config import Settings


def test_frontend_dist_default_empty():
    assert Settings().frontend_dist == ""


def test_frontend_dist_from_env(monkeypatch):
    monkeypatch.setenv("FRONTEND_DIST", "/tmp/dist")
    assert Settings().frontend_dist == "/tmp/dist"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_config_frontend_dist.py -v`
Expected: FAIL (`frontend_dist` missing).

- [ ] **Step 3: Add the setting**

In `backend/app/config.py`, immediately after the `history_cache_ttl_hours` line, add:
```python
    # When set (the desktop bundle sets FRONTEND_DIST), the API also serves the built
    # React app from this directory at "/" with SPA fallback. Empty in dev (Vite serves UI).
    frontend_dist: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_config_frontend_dist.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_config_frontend_dist.py
git commit -m "feat(config): add frontend_dist setting for packaged UI serving

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: SPA static mount

**Files:**
- Create: `backend/app/static.py`
- Modify: `backend/app/main.py` (call `mount_frontend` after routers)
- Test: `backend/tests/test_static_mount.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_static_mount.py
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.static import mount_frontend


def _make_dist(tmp_path: Path) -> str:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>App</title>")
    (tmp_path / "assets" / "main.js").write_text("console.log('hi')")
    return str(tmp_path)


def _app(dist: str) -> FastAPI:
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    mount_frontend(app, dist)
    return app


def test_serves_index_and_spa_fallback(tmp_path):
    client = TestClient(_app(_make_dist(tmp_path)))
    assert client.get("/").status_code == 200
    assert "<title>App</title>" in client.get("/").text
    # client-side route -> SPA fallback to index.html
    r = client.get("/strategies")
    assert r.status_code == 200 and "<title>App</title>" in r.text


def test_api_routes_not_shadowed(tmp_path):
    client = TestClient(_app(_make_dist(tmp_path)))
    assert client.get("/api/ping").json() == {"ok": True}


def test_asset_miss_is_404_not_html(tmp_path):
    client = TestClient(_app(_make_dist(tmp_path)))
    assert client.get("/assets/main.js").status_code == 200
    assert client.get("/assets/missing.js").status_code == 404


def test_noop_when_dist_missing():
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    mount_frontend(app, "")  # disabled
    client = TestClient(app)
    assert client.get("/api/ping").json() == {"ok": True}
    assert client.get("/").status_code == 404   # nothing mounted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_static_mount.py -v`
Expected: FAIL (module `app.static` missing).

- [ ] **Step 3: Implement `app/static.py`**

```python
# backend/app/static.py
"""Serve the built React app same-origin from the FastAPI process (packaged desktop
build). No-op unless ``dist_dir`` is set and exists, so the dev workflow (Vite) is
untouched. Mount this AFTER all /api and /ws routers so it never shadows them."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_frontend(app: FastAPI, dist_dir: str) -> None:
    if not dist_dir:
        return
    dist = Path(dist_dir)
    index = dist / "index.html"
    if not index.exists():
        return

    assets = dist / "assets"
    if assets.is_dir():
        # Hashed bundles; a miss here returns 404 (never the SPA fallback).
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # API/WS are matched by earlier routers; guard just in case.
        if full_path.startswith(("api/", "ws")):
            raise HTTPException(status_code=404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))   # e.g. favicon, vite.svg
        return FileResponse(str(index))           # SPA fallback
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_static_mount.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire into `main.py` (after all routers)**

In `backend/app/main.py`, after the last `app.include_router(...)` line (the ws router), add:
```python
from app.static import mount_frontend
mount_frontend(app, settings.frontend_dist)
```
(Place it below the router includes so the catch-all is registered last.)

- [ ] **Step 6: Verify app still imports + full suite green**

Run: `../.venv/bin/python -c "import app.main; print('ok')" && ../.venv/bin/python -m pytest -q`
Expected: prints `ok`; all tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/static.py backend/app/main.py backend/tests/test_static_mount.py
git commit -m "feat(static): serve built React app same-origin with SPA fallback

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Same-origin WebSocket URL

**Files:**
- Modify: `frontend/src/api.js:167-169` (`wsUrl`)

- [ ] **Step 1: Update `wsUrl` to handle an empty API_BASE**

Replace the `wsUrl` function in `frontend/src/api.js` with:
```javascript
export function wsUrl() {
  // Packaged build uses VITE_API_BASE="" (same-origin) -> derive from the page.
  if (!API_BASE) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }
  return API_BASE.replace(/^http/, "ws") + "/ws";
}
```

- [ ] **Step 2: Verify dev build still compiles**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Verify the packaged (same-origin) build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build`
Expected: build succeeds; `dist/` produced.

- [ ] **Step 4: Commit**

```bash
cd /Users/karanpatel/PycharmProjects/Tradding-system
git add frontend/src/api.js
git commit -m "feat(ui): same-origin wsUrl when API_BASE is empty (packaged build)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Launcher — `desktop/app.py`

**Files:**
- Create: `desktop/app.py`
- Create: `desktop/__init__.py` (empty, so tests can import `desktop.app`)
- Test: `backend/tests/test_desktop_launcher.py`

Pure helpers are unit-tested; the uvicorn thread + native window are exercised only by the
manual build (Task 7). `webview` is imported lazily inside `main()` so the helpers import
without it.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_desktop_launcher.py
import sys
from pathlib import Path

# Make the repo root importable so `import desktop.app` works from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from desktop import app as launcher


def test_resolve_data_dir_creates_app_support(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = launcher.resolve_data_dir()
    assert d.exists() and d.is_dir()
    assert d == tmp_path / "Library" / "Application Support" / "TradingSystem"


def test_set_runtime_env_points_db_and_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HISTORY_CACHE_DIR", raising=False)
    monkeypatch.delenv("FRONTEND_DIST", raising=False)
    launcher.set_runtime_env(tmp_path, "/some/dist")
    import os
    assert os.environ["DATABASE_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'trading.db'}"
    assert os.environ["HISTORY_CACHE_DIR"] == str(tmp_path / "data_cache")
    assert os.environ["FRONTEND_DIST"] == "/some/dist"


def test_wait_for_server_false_on_dead_port():
    # Nothing listening on this port -> returns False quickly within the budget.
    assert launcher.wait_for_server("http://127.0.0.1:8766/health", timeout=1.0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_desktop_launcher.py -v`
Expected: FAIL (`desktop.app` missing).

- [ ] **Step 3: Implement `desktop/app.py`**

```python
# desktop/app.py
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
```
Also create an empty `desktop/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_desktop_launcher.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add desktop/app.py desktop/__init__.py backend/tests/test_desktop_launcher.py
git commit -m "feat(desktop): PyWebview launcher (uvicorn thread + native window)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: PyInstaller spec + build script

**Files:**
- Create: `desktop/TradingSystem.spec`
- Create: `desktop/build.sh`

- [ ] **Step 1: Write the PyInstaller spec**

Create `desktop/TradingSystem.spec`:
```python
# -*- mode: python ; coding: utf-8 -*-
# Build: cd desktop && ../.venv/bin/pyinstaller TradingSystem.spec
from PyInstaller.utils.hooks import collect_submodules, collect_all

block_cipher = None
ROOT = os.path.abspath(os.path.join(os.getcwd(), ".."))

datas = [
    (os.path.join(ROOT, "frontend", "dist"), "frontend_dist"),
    # Bundle ONLY the app package (not the whole backend/ — that holds data_cache,
    # trading.db and tests we don't want in the .app).
    (os.path.join(ROOT, "backend", "app"), "app"),
]
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("app")
    + ["aiosqlite", "pydantic_settings", "apscheduler", "websockets"]
)
for pkg in ("ib_async", "sqlalchemy", "fastapi", "starlette", "pydantic"):
    d, b, h = collect_all(pkg)
    datas += d
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "desktop", "app.py")],
    pathex=[os.path.join(ROOT, "backend")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TradingSystem",
          console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="TradingSystem")
app = BUNDLE(coll, name="TradingSystem.app", icon=None,
             bundle_identifier="com.local.tradingsystem")
```

- [ ] **Step 2: Write the build script**

Create `desktop/build.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin"

echo "==> Building frontend (same-origin)"
( cd "$ROOT/frontend" && VITE_API_BASE="" npm run build )

echo "==> Bundling with PyInstaller"
( cd "$ROOT/desktop" && "$PY/pyinstaller" --noconfirm --clean TradingSystem.spec )

echo "==> Done: $ROOT/desktop/dist/TradingSystem.app"
```

- [ ] **Step 3: Make the build script executable + ensure spec imports os**

At the very top of `desktop/TradingSystem.spec`, add `import os` as the first line (PyInstaller
runs the spec as Python; `os` is used above).
Run: `chmod +x desktop/build.sh`

- [ ] **Step 4: Commit**

```bash
git add desktop/TradingSystem.spec desktop/build.sh
git commit -m "build(desktop): PyInstaller spec + build script

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Build & verification

**Files:** none (build + manual checks)

- [ ] **Step 1: Full backend suite green**

Run: `cd backend && ../.venv/bin/python -m pytest -q`
Expected: all tests pass (includes the new config/static/launcher tests).

- [ ] **Step 2: Run the build**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system && ./desktop/build.sh`
Expected: completes and prints the path to `desktop/dist/TradingSystem.app`. If PyInstaller
reports a missing module at first launch (Step 4), add it to `hiddenimports` in the spec and
rebuild.

- [ ] **Step 3: Sanity-check the bundle exists**

Run: `ls -d desktop/dist/TradingSystem.app && ls desktop/dist/TradingSystem.app/Contents/MacOS`
Expected: the `.app` and a `TradingSystem` executable exist.

- [ ] **Step 4: Manual launch (user)**

Run: `open desktop/dist/TradingSystem.app` (first time: right-click → Open to clear Gatekeeper).
Expected: a native window opens showing the dashboard; the API tab connects to IB Gateway
(`127.0.0.1:4002`). If the window is blank, run the binary from a terminal to see logs:
`./desktop/dist/TradingSystem.app/Contents/MacOS/TradingSystem`.

- [ ] **Step 5: Commit any spec fixes**

```bash
git add -A desktop
git commit -m "build(desktop): finalize bundle (hidden-import fixes)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
