# Mac Desktop App (single bundle) — Design Spec

Date: 2026-06-18
Status: Approved — implement.

## Goal
Ship one double-clickable `TradingSystem.app` for macOS that runs the FastAPI backend
**and** serves the built React dashboard in a native window — no terminal, no separate
Vite/uvicorn processes. Personal use; unsigned.

## Decisions (locked)
1. **Packaging:** PyWebView (native WKWebView window) + PyInstaller (one `.app`). All-Python.
2. **Distribution:** personal, **unsigned** (first launch: right-click → Open past Gatekeeper).
3. **Single process:** uvicorn runs in-process (daemon thread); FastAPI serves the UI; the
   webview loads one local origin.
4. **Scope:** the app is the **live cockpit** (dashboard, strategies, connect IBKR). The IBKR
   Gateway stays a separate app (IBKR's own software — not bundleable); the app connects to it
   at `127.0.0.1:4002`. The `data_cache` backtest bars are **NOT bundled** (keeps the .app
   lean); backtests run from the source checkout or by pointing the data dir at a copy.

## Architecture

### Launcher — `desktop/app.py`
- Resolve a writable data dir: `~/Library/Application Support/TradingSystem/` (created if
  missing). Set env **before importing the app** so config picks it up:
  `DATABASE_URL=sqlite+aiosqlite:///<dir>/trading.db`, `HISTORY_CACHE_DIR=<dir>/data_cache`,
  and `FRONTEND_DIST=<bundled dist path>` (enables the static mount).
- Start uvicorn for `app.main:app` on `127.0.0.1:8765` in a **daemon thread**.
- Poll `http://127.0.0.1:8765/health` (short timeout, ~10s budget) until ready.
- Open a PyWebView window (title "Trading System", ~1400×900) at that URL; `webview.start()`
  blocks on the macOS main thread. Closing the window ends the process.

### Same-origin UI serving — backend
- New `app/static.py`: if `settings.frontend_dist` is set and exists, mount it so `/` serves
  `index.html`, hashed assets serve normally, and **unknown non-API paths fall back to
  `index.html`** (SPA). Mounted **after** all `/api/*` and `/ws` routers so it never shadows
  them. No-op when `frontend_dist` is empty (dev mode unaffected).
- `app/config.py`: add `frontend_dist: str = ""` (env `FRONTEND_DIST`).
- Frontend built with `VITE_API_BASE=""` → fetch + WS are same-origin. Fix `api.js`
  `wsUrl()` to derive from `window.location` when `API_BASE` is empty:
  `const p = location.protocol === "https:" ? "wss" : "ws"; return \`${p}://${location.host}/ws\`;`

### Packaging — `desktop/TradingSystem.spec` + `desktop/build.sh`
- `build.sh`: `(cd frontend && VITE_API_BASE="" npm run build)` → run PyInstaller on the spec
  → `desktop/dist/TradingSystem.app`.
- Spec: entry `desktop/app.py`; `datas` includes the built `frontend/dist` (→ bundled, path
  exposed to the launcher) and any package data; `hiddenimports`/`collect_all` for
  ib_async, uvicorn (+ `uvicorn.logging`, `uvicorn.loops`, `uvicorn.protocols`), fastapi,
  starlette, pydantic, pydantic_settings, sqlalchemy, aiosqlite, apscheduler, websockets.
  `windowed=True`, `name="TradingSystem"`, macOS `.app` bundle.

### Writable paths (bundle is read-only)
- All runtime writes (SQLite DB, any history cache writes, `.session_store.json` is gone) go
  to the Application Support dir via the env vars the launcher sets. The read-only bundle holds
  only code + `dist/`.

## File structure
- `desktop/app.py` — launcher (data dir, uvicorn thread, webview window).
- `desktop/TradingSystem.spec` — PyInstaller spec.
- `desktop/build.sh` — build script (frontend build + pyinstaller).
- `desktop/requirements-desktop.txt` — `pywebview`, `pyinstaller` (dev/build-only deps).
- `backend/app/static.py` — SPA static mount helper; called from `main.py`.
- `backend/app/config.py` — add `frontend_dist` setting.
- `frontend/src/api.js` — same-origin `wsUrl()`.

## Testing
- **Unit (no bundling needed):**
  - `frontend_dist` static mount via FastAPI `TestClient`: `GET /` → 200 + HTML; `GET
    /api/health`-style route still routes to the API; `GET /some/client/route` → index.html
    (SPA fallback); a `/assets/...` 404 stays 404 (don't fall back asset misses to HTML).
  - data-dir env resolution: importing config with `DATABASE_URL`/`HISTORY_CACHE_DIR` set
    yields those values.
- **Manual (user, on the Mac):** run `desktop/build.sh`, then `open desktop/dist/TradingSystem.app`
  — the window opens, the dashboard loads, the API tab connects to IB Gateway. The agent can
  run the build but cannot open/verify the native window.

## Out of scope
- Code signing / notarization / distribution to other Macs.
- Bundling `data_cache` / offline backtests in the app.
- Auto-launching or bundling the IBKR Gateway.
- Auto-update, tray icon, native menus beyond defaults.

## Risks
- PyInstaller hidden-import gaps (uvicorn/sqlalchemy/ib_async dynamic imports) — the spec lists
  the known ones; surface any `ModuleNotFoundError` at first launch and add to the spec.
- macOS Gatekeeper blocks unsigned first launch — expected; right-click → Open once.
- Port 8765 in use — acceptable for personal use; fixed for simplicity.

## Notes
Repo is under git (initialized during the IBKR migration). Spec on disk.
