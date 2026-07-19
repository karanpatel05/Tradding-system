# Live-Orders Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A UI switch (API tab) that flips the `ENABLE_LIVE_ORDERS` gate at runtime, persisted to `backend/.env`, with a PAPER/LIVE badge on the dashboard.

**Architecture:** Mirror the credentials pattern in `app/auth/routes.py`: mutate the `lru_cache`d `Settings` object in place AND persist via `_upsert_env`. All order-gate checks call `get_settings().enable_live_orders` at call time, so the in-place mutation takes effect immediately. One new endpoint `PUT /api/system/live-orders`; frontend reads state from the ungated `/health` endpoint which already reports `live_orders_enabled`.

**Tech Stack:** FastAPI + pydantic-settings (backend), React 18 + Vite (frontend). Backend tests are standalone scripts run as `python -m tests.<name>` (see `tests/test_resume_endpoint.py` for the pattern — NOT pytest).

**Spec:** `docs/superpowers/specs/2026-07-19-live-orders-switch-design.md`

## Global Constraints

- Persist first, flip second: if the `.env` write fails, the in-memory gate must NOT be left flipped.
- Turning OFF is always instant (plain `PUT {"enabled": false}`); the type-`LIVE` confirmation is frontend-only friction.
- `PUT /api/system/live-orders` must be guarded by `require_auth` (same as `/api/system/resume`).
- Do NOT touch SAFE_MODE, the kill switch, or any gate-check call sites.
- All commands run from repo root `/Users/karanpatel/PycharmProjects/Tradding-system`; the venv python is `.venv/bin/python`.

---

### Task 1: Backend endpoint `PUT /api/system/live-orders`

**Files:**
- Modify: `backend/app/routers/system.py`
- Test: `backend/tests/test_live_orders_switch.py` (create)

**Interfaces:**
- Consumes: `_upsert_env(path, values)` and `_ENV_PATH` from `app/auth/routes.py`; `get_settings()` from `app/config.py`; `require_auth` from `app/auth/gate.py`.
- Produces: `PUT /api/system/live-orders` accepting `{"enabled": bool}`, returning `{"live_orders_enabled": bool}`. Module-level names in `app.routers.system`: `LiveOrdersBody` (pydantic model), `set_live_orders` (endpoint fn), `_ENV_PATH` (re-imported binding, monkeypatchable). Tasks 2–3 rely on the URL + JSON shapes exactly.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_live_orders_switch.py`:

```python
"""PUT /api/system/live-orders flips the cached setting + persists to .env.
   Run: python -m tests.test_live_orders_switch
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


def _run() -> None:
    async def _t():
        from app.auth.gate import require_auth
        from app.config import get_settings
        from app.routers import system as sys_mod

        # Redirect .env writes to a temp file (never touch the real backend/.env).
        tmp = Path(tempfile.mkdtemp()) / ".env"
        tmp.write_text("EXISTING=1\n")
        sys_mod._ENV_PATH = tmp

        s = get_settings()
        assert s.enable_live_orders is False, "test expects the default (gate OFF)"

        # ON: returns new state, flips the cached singleton, persists to .env.
        res = await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=True))
        assert res == {"live_orders_enabled": True}, res
        assert get_settings().enable_live_orders is True
        text = tmp.read_text()
        assert "ENABLE_LIVE_ORDERS=true" in text and "EXISTING=1" in text, text

        # OFF: plain false body, instant.
        res = await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=False))
        assert res == {"live_orders_enabled": False}, res
        assert get_settings().enable_live_orders is False
        assert "ENABLE_LIVE_ORDERS=false" in tmp.read_text()

        # A failed .env write must NOT leave the in-memory gate flipped.
        sys_mod._ENV_PATH = Path("/nonexistent-dir-for-test/.env")
        try:
            await sys_mod.set_live_orders(sys_mod.LiveOrdersBody(enabled=True))
            raised = False
        except OSError:
            raised = True
        assert raised, "expected OSError from unwritable .env path"
        assert get_settings().enable_live_orders is False, \
            "gate flipped despite persist failure"

        # Route must be auth-gated like /resume.
        route = next(r for r in sys_mod.router.routes
                     if r.path == "/api/system/live-orders")
        assert require_auth in [d.call for d in route.dependant.dependencies], \
            "PUT /api/system/live-orders is missing require_auth"
        print("PASS: live-orders switch endpoint")
    asyncio.run(_t())
    print("\nALL LIVE-ORDERS-SWITCH TESTS PASSED")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m tests.test_live_orders_switch`
Expected: FAIL with `AttributeError: module 'app.routers.system' has no attribute '_ENV_PATH'` (or `LiveOrdersBody`).

- [ ] **Step 3: Implement the endpoint**

In `backend/app/routers/system.py`, replace the imports block at the top:

```python
"""System ops endpoints: resume from SAFE_MODE (clear flag + re-run recovery),
live-orders master switch (ENABLE_LIVE_ORDERS, persisted to backend/.env)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.gate import require_auth
from app.auth.routes import _ENV_PATH, _upsert_env
from app.broker.status import is_ready_cached, probe
from app.config import get_settings
from app.engine.recovery import run_recovery
from app.engine.safe_mode import clear_safe_mode, is_safe_mode

logger = logging.getLogger("trading.system")

router = APIRouter(prefix="/api/system", tags=["system"])
```

Then append at the end of the file:

```python
class LiveOrdersBody(BaseModel):
    enabled: bool


@router.put("/live-orders", dependencies=[Depends(require_auth)])
async def set_live_orders(body: LiveOrdersBody) -> dict:
    """Flip the ENABLE_LIVE_ORDERS master gate at runtime.

    Persist to backend/.env FIRST: if the write fails, the in-memory gate must
    not disagree with what survives a restart. The type-LIVE confirmation is
    frontend-only; this endpoint takes a plain boolean (auth-gated)."""
    s = get_settings()
    old = s.enable_live_orders
    _upsert_env(_ENV_PATH, {"ENABLE_LIVE_ORDERS": "true" if body.enabled else "false"})
    s.enable_live_orders = body.enabled
    if old != body.enabled:
        logger.warning(
            "[LIVE-ORDERS] switched %s -> %s (%s)",
            "ON" if old else "OFF", "ON" if body.enabled else "OFF",
            "REAL ORDERS ARMED" if body.enabled else "paper mode",
        )
    return {"live_orders_enabled": s.enable_live_orders}
```

Note: `from app.auth.routes import _ENV_PATH` copies the binding into `app.routers.system`, which is exactly what lets the test monkeypatch `sys_mod._ENV_PATH` without affecting the auth module.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m tests.test_live_orders_switch`
Expected: `PASS: live-orders switch endpoint` then `ALL LIVE-ORDERS-SWITCH TESTS PASSED`

- [ ] **Step 5: Regression — the resume endpoint still imports/works**

Run: `cd backend && DATABASE_URL=sqlite+aiosqlite:///./_t_re.db ../.venv/bin/python -m tests.test_resume_endpoint`
Expected: `ALL RESUME-ENDPOINT TESTS PASSED`

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/system.py backend/tests/test_live_orders_switch.py
git commit -m "feat(system): PUT /api/system/live-orders — runtime ENABLE_LIVE_ORDERS switch persisted to .env"
```

---

### Task 2: API client + dashboard PAPER/LIVE chip

**Files:**
- Modify: `frontend/src/api.js` (add two methods to the `api` object, near the existing `resume:` line)
- Modify: `frontend/src/components/DataBrokerChips.jsx`

**Interfaces:**
- Consumes: `GET /health` (ungated) returning `{status, app, broker, live_orders_enabled}`; `PUT /api/system/live-orders` from Task 1.
- Produces: `api.health()` and `api.setLiveOrders(enabled)` — Task 3 calls both with these exact names.

- [ ] **Step 1: Add API methods**

In `frontend/src/api.js`, directly under the `resume:` line inside the `api` object, add:

```js
  health: () => request("/health"),
  // Master ENABLE_LIVE_ORDERS gate. Confirmation UX lives in the frontend;
  // the endpoint takes a plain boolean.
  setLiveOrders: (enabled) =>
    request("/api/system/live-orders", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
```

- [ ] **Step 2: Add the Orders chip**

In `frontend/src/components/DataBrokerChips.jsx`, add health state and a third chip. Full updated component:

```jsx
import { useEffect, useState } from "react";
import { api } from "../api.js";

// Dashboard header chips separating the two health signals that used to be conflated:
// Data (which market-data source is actually serving) and Broker (orders), plus the
// order-routing mode (paper vs LIVE — the ENABLE_LIVE_ORDERS master gate).
const DATA_LABEL = { tastytrade: "Tastytrade" };

export default function DataBrokerChips() {
  const [tt, setTt] = useState(null);
  const [auth, setAuth] = useState(null);
  const [health, setHealth] = useState(null);

  useEffect(() => {
    const load = () => {
      api.ttStatus().then(setTt).catch(() => setTt(null));
      api.authStatus().then(setAuth).catch(() => setAuth(null));
      api.health().then(setHealth).catch(() => setHealth(null));
    };
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const dataTone = tt == null ? "idle"
    : tt.serving === "tastytrade" ? "live"
    : tt.configured ? "idle" : "down";
  const dataText = tt == null ? "…"
    : tt.serving === "tastytrade" ? DATA_LABEL[tt.serving]
    : tt.configured ? "Tastytrade · idle" : "not configured";

  const brokerReady = tt?.probe === "ok";
  const brokerText = tt == null ? "…"
    : auth?.conn_state === "safe_mode" ? "SAFE MODE"
    : brokerReady ? "Tastytrade verified"
    : "not verified";

  const liveOrders = !!health?.live_orders_enabled;

  return (
    <div className="src-chips">
      <span className={`pill tiny ${dataTone}`}>Data: {dataText}</span>
      <span className={`pill tiny ${brokerReady ? "live" : "down"}`}>Broker: {brokerText}</span>
      <span className={`pill tiny ${liveOrders ? "down" : "idle"}`}>
        Orders: {liveOrders ? "LIVE" : "PAPER"}
      </span>
    </div>
  );
}
```

(`pill tiny down` renders red — correct attention color for LIVE; `idle` is muted for PAPER. Both classes already exist in `styles.css` and are used in this same file.)

- [ ] **Step 3: Build to catch syntax errors**

Run: `cd frontend && npm run build`
Expected: `vite build` completes with no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.js frontend/src/components/DataBrokerChips.jsx
git commit -m "feat(dashboard): Orders PAPER/LIVE chip + api.health()/setLiveOrders()"
```

---

### Task 3: API-tab "Order Routing" card with type-LIVE confirm

**Files:**
- Modify: `frontend/src/components/ApiTab.jsx`

**Interfaces:**
- Consumes: `api.health()` → `{live_orders_enabled}` and `api.setLiveOrders(enabled)` → `{live_orders_enabled}` from Task 2.
- Produces: UI only; nothing downstream consumes it.

- [ ] **Step 1: Add state + handlers**

In `frontend/src/components/ApiTab.jsx`, after the existing `tt` state block (after the `saveTt` function, before `reprobe`), add:

```jsx
  // --- Order routing (ENABLE_LIVE_ORDERS master gate) ---
  const [health, setHealth] = useState(null);
  const [arming, setArming] = useState(false);      // confirm row visible
  const [confirmText, setConfirmText] = useState("");
  const [ordersMsg, setOrdersMsg] = useState(null);
  useEffect(() => { api.health().then(setHealth).catch(() => {}); }, []);

  const liveOn = !!health?.live_orders_enabled;
  const setLive = async (enabled) => {
    setOrdersMsg(null);
    try {
      const res = await api.setLiveOrders(enabled);
      setHealth((h) => ({ ...(h || {}), live_orders_enabled: res.live_orders_enabled }));
      setArming(false);
      setConfirmText("");
      onChange?.();
    } catch (e) {
      setOrdersMsg(e.message || "Update failed");
    }
  };
```

- [ ] **Step 2: Add the card**

In the same file, after the closing `</section>` of the "Tastytrade Credentials" card (before the final `</div>`), add:

```jsx
      <section className="card">
        <div className="card-head">
          <h2>Order Routing</h2>
          <span className={`mode-badge ${liveOn ? "danger" : "disabled"}`}>
            {liveOn ? "LIVE" : "PAPER"}
          </span>
        </div>
        <p className="muted small">
          Master safety gate (<code>ENABLE_LIVE_ORDERS</code>). In <b>paper</b> mode
          strategies run their full logic but orders are logged, never transmitted.
          In <b>LIVE</b> mode real orders go to Tastytrade. The setting persists in{" "}
          <code>.env</code> across restarts. SAFE MODE and the kill switch are unaffected.
        </p>
        {!liveOn && !arming && (
          <button className="btn primary" style={{ marginTop: 10 }}
            onClick={() => setArming(true)}>
            Enable live orders…
          </button>
        )}
        {!liveOn && arming && (
          <div className="row gap center" style={{ marginTop: 10 }}>
            <input className="input" placeholder="Type LIVE to arm" value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)} />
            <button className="btn danger" disabled={confirmText !== "LIVE"}
              onClick={() => setLive(true)}>
              Arm live orders
            </button>
            <button className="btn ghost"
              onClick={() => { setArming(false); setConfirmText(""); }}>
              Cancel
            </button>
          </div>
        )}
        {liveOn && (
          <button className="btn" style={{ marginTop: 10 }} onClick={() => setLive(false)}>
            Switch to paper
          </button>
        )}
        {ordersMsg && <div className="banner error" style={{ marginTop: 10 }}>{ordersMsg}</div>}
      </section>
```

Check `styles.css` for a `.btn.danger` rule; if it doesn't exist, use `className="btn primary"` for the arm button instead (the disabled-until-typed gate is the real guard).

- [ ] **Step 3: Build to catch syntax errors**

Run: `cd frontend && npm run build`
Expected: `vite build` completes with no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ApiTab.jsx
git commit -m "feat(api-tab): Order Routing card — type-LIVE confirm to arm, one-click back to paper"
```

---

### Task 4: End-to-end verification

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything above, running against the real backend.

- [ ] **Step 1: Backend test suite still green**

Run: `cd backend && ../.venv/bin/python -m tests.test_live_orders_switch`
Expected: `ALL LIVE-ORDERS-SWITCH TESTS PASSED`

- [ ] **Step 2: Restart the running backend so it picks up the new code**

The dev server runs WITHOUT --reload. Find and restart it:

```bash
kill $(lsof -tiTCP:8765 -sTCP:LISTEN)
sleep 2
cd /Users/karanpatel/PycharmProjects/Tradding-system
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765 \
  --app-dir backend > /tmp/uvicorn-8765.log 2>&1 &
sleep 5
curl -s http://localhost:8765/health
```

Expected: `{"status":"ok",...,"live_orders_enabled":false}`

- [ ] **Step 3: Verify the endpoint is live and auth-gated**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X PUT http://localhost:8765/api/system/live-orders \
  -H "Content-Type: application/json" -d '{"enabled": true}'
```

Expected: `401` (auth required, no session cookie) — proves the route exists and is gated. Confirm `backend/.env` was NOT modified: `grep -c ENABLE_LIVE_ORDERS backend/.env || echo absent`.

- [ ] **Step 4: Drive the UI**

Rebuild/serve the frontend (`cd frontend && npm run dev` if not already served by the backend build) and in the browser: API tab → Order Routing card shows PAPER → "Enable live orders…" → type `LIVE` → Arm → badge flips to LIVE, dashboard chip shows `Orders: LIVE`, `backend/.env` contains `ENABLE_LIVE_ORDERS=true`, backend log shows `[LIVE-ORDERS] switched OFF -> ON`. Then click "Switch to paper" → everything reverts and `.env` shows `ENABLE_LIVE_ORDERS=false`. **Leave the gate OFF (paper) when done — the account is unfunded and live trading is not yet cleared.**

- [ ] **Step 5: Final commit (if any fixups were needed)**

```bash
git status --short   # commit any fixups with a descriptive message
```
