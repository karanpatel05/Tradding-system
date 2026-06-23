# Account Info on Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the IBKR account summary (balances, P&L, margin) live on the dashboard.

**Architecture:** `app/ibkr/account.py` gains a pure `normalize_account_summary` + async `get_account_summary` (via `ib.accountSummaryAsync`). The WS snapshot adds an `account` key (refreshed on the positions cadence); a `GET /api/account` REST endpoint mirrors it. The React dashboard renders an Account card from `snapshot.account`.

**Tech Stack:** Python 3.14, FastAPI, ib_async, React/Vite, pytest.

**Spec:** `docs/superpowers/specs/2026-06-23-account-info-dashboard-design.md`

> Run `pytest` from `backend/`. Python: `/Users/karanpatel/PycharmProjects/Tradding-system/.venv/bin/python`. Repo under git; commit messages end with the Co-Authored-By trailer.

---

### Task 1: Account summary in `app/ibkr/account.py`

**Files:**
- Modify: `backend/app/ibkr/account.py`
- Test: `backend/tests/test_account_summary.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_account_summary.py
from app.ibkr.account import normalize_account_summary


class _V:  # mimics ib_async AccountValue
    def __init__(self, tag, value, currency="USD", account="DU1"):
        self.tag, self.value, self.currency, self.account = tag, value, currency, account


def test_normalize_maps_tags_and_parses_floats():
    rows = [
        _V("NetLiquidation", "2000.50"), _V("TotalCashValue", "1500.25"),
        _V("BuyingPower", "8000"), _V("AvailableFunds", "1900"),
        _V("GrossPositionValue", "500"), _V("UnrealizedPnL", "-12.5"),
        _V("RealizedPnL", "7.0"), _V("InitMarginReq", "100"),
        _V("MaintMarginReq", "80"), _V("AccountType", "INDIVIDUAL"),  # ignored
    ]
    s = normalize_account_summary(rows)
    assert s["net_liquidation"] == 2000.50 and s["total_cash"] == 1500.25
    assert s["buying_power"] == 8000.0 and s["available_funds"] == 1900.0
    assert s["gross_position_value"] == 500.0
    assert s["unrealized_pnl"] == -12.5 and s["realized_pnl"] == 7.0
    assert s["init_margin"] == 100.0 and s["maint_margin"] == 80.0
    assert s["account"] == "DU1" and s["currency"] == "USD"


def test_normalize_missing_tags_default_none():
    s = normalize_account_summary([_V("NetLiquidation", "100")])
    assert s["net_liquidation"] == 100.0
    assert s["buying_power"] is None and s["realized_pnl"] is None


def test_normalize_empty_input():
    s = normalize_account_summary([])
    assert s["account"] is None and s["currency"] is None
    assert all(s[k] is None for k in ("net_liquidation", "total_cash", "buying_power"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_account_summary.py -v`
Expected: FAIL (`normalize_account_summary` missing).

- [ ] **Step 3: Implement in `app/ibkr/account.py`**

Append to `backend/app/ibkr/account.py`:
```python
_ACCOUNT_TAGS = {
    "NetLiquidation": "net_liquidation", "TotalCashValue": "total_cash",
    "BuyingPower": "buying_power", "AvailableFunds": "available_funds",
    "GrossPositionValue": "gross_position_value", "UnrealizedPnL": "unrealized_pnl",
    "RealizedPnL": "realized_pnl", "InitMarginReq": "init_margin",
    "MaintMarginReq": "maint_margin",
}


def normalize_account_summary(values) -> dict:
    """IBKR accountSummary rows -> normalized {key: float|None} + account/currency."""
    out: dict = {key: None for key in _ACCOUNT_TAGS.values()}
    out["account"] = None
    out["currency"] = None
    for v in values:
        if out["account"] is None:
            out["account"] = getattr(v, "account", None) or None
        if out["currency"] is None and getattr(v, "currency", ""):
            out["currency"] = v.currency
        key = _ACCOUNT_TAGS.get(getattr(v, "tag", None))
        if key is not None:
            try:
                out[key] = float(v.value)
            except (TypeError, ValueError):
                pass
    return out


async def get_account_summary(account: str | None = None) -> dict:
    """Full IBKR account summary, normalized. Returns {} when not connected."""
    if not get_gateway().is_connected():
        return {}
    ib = await get_gateway().get_ib()
    rows = await ib.accountSummaryAsync(account or "")
    return normalize_account_summary(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_account_summary.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/ibkr/account.py backend/tests/test_account_summary.py
git commit -m "feat(ibkr): account summary (balances/PnL/margin) normalizer + fetch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Expose via WS snapshot + REST

**Files:**
- Modify: `backend/app/routers/ws.py`
- Modify: `backend/app/routers/dashboard.py`

- [ ] **Step 1: Add the account snapshot helper + key in `ws.py`**

In `backend/app/routers/ws.py`, add to the imports (near `from app.ibkr.account import get_positions`):
```python
from app.ibkr.account import get_account_summary, get_positions
```
(Replace the existing `from app.ibkr.account import get_positions` line with the line above.)

Add this helper next to `_positions_snapshot` (above the websocket endpoint):
```python
async def _account_snapshot() -> dict:
    if not session_manager.is_authenticated():
        return {}
    try:
        return await get_account_summary()
    except Exception as exc:  # noqa: BLE001
        logger.debug("WS account fetch failed: %s", exc)
        return {}
```

In the push loop, add a `broker_account` cache initialized with the other locals
(`broker_positions: list[dict] = []`) — add right after it:
```python
    broker_account: dict = {}
```
Then inside the `if tick % POSITIONS_EVERY == 0:` block, after
`broker_positions = await _positions_snapshot()`, add:
```python
                broker_account = await _account_snapshot()
```
And add `"account": broker_account,` to the `websocket.send_json({...})` dict (next to
`"positions": positions,`).

- [ ] **Step 2: Add the REST endpoint in `dashboard.py`**

In `backend/app/routers/dashboard.py`, update the account import:
```python
from app.ibkr.account import get_account_summary, get_positions
```
(Replace the existing `from app.ibkr.account import get_positions` line.)

Add this endpoint (after the `positions()` endpoint):
```python
@router.get("/account", dependencies=[Depends(require_auth)])
async def account() -> dict:
    """IBKR account summary (balances/PnL/margin). Degrades gracefully."""
    if not session_manager.is_authenticated():
        return {"account": {}, "note": "Not connected to IBKR."}
    try:
        return {"account": await get_account_summary(), "note": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Account summary fetch failed: %s", exc)
        return {"account": {}, "note": "Account feed temporarily unavailable."}
```

- [ ] **Step 3: Verify backend imports + suite green**

Run: `../.venv/bin/python -c "import app.main; print('ok')" && ../.venv/bin/python -m pytest -q`
Expected: prints `ok`; all tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/ws.py backend/app/routers/dashboard.py
git commit -m "feat(dashboard): expose account summary via WS snapshot + /api/account

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Account card on the dashboard

**Files:**
- Modify: `frontend/src/api.js` (add `account` REST helper)
- Modify: `frontend/src/components/Dashboard.jsx`

- [ ] **Step 1: Add the REST helper in `api.js`**

In `frontend/src/api.js`, in the `api` object (next to `positions:` / market entries), add:
```javascript
  account: () => request("/api/account"),
```

- [ ] **Step 2: Add the Account card to `Dashboard.jsx`**

Open `frontend/src/components/Dashboard.jsx`. Near the top of the file (after imports), add a
currency formatter helper:
```javascript
const money = (v) =>
  v === null || v === undefined ? "—" : v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const money2 = (v) =>
  v === null || v === undefined ? "—" : v.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
```

Inside the `Dashboard` component's returned JSX, add this `<section>` as the first card in the
main grid (immediately after the opening grid container, before the existing market card):
```jsx
        <section className="card">
          <div className="card-head">
            <h2>Account</h2>
            <span className="muted small">{snapshot?.account?.account || "IBKR"}</span>
          </div>
          {snapshot?.account && Object.keys(snapshot.account).length > 0 ? (
            <div className="deploy-grid" style={{ marginTop: 12 }}>
              <div><div className="stat-label">Net Liquidation</div><div className="stat-value">{money(snapshot.account.net_liquidation)}</div></div>
              <div><div className="stat-label">Cash</div><div className="stat-value">{money(snapshot.account.total_cash)}</div></div>
              <div><div className="stat-label">Buying Power</div><div className="stat-value">{money(snapshot.account.buying_power)}</div></div>
              <div><div className="stat-label">Available Funds</div><div className="stat-value">{money(snapshot.account.available_funds)}</div></div>
              <div><div className="stat-label">Position Value</div><div className="stat-value">{money(snapshot.account.gross_position_value)}</div></div>
              <div><div className="stat-label">Unrealized P&L</div><div className={`stat-value ${snapshot.account.unrealized_pnl >= 0 ? "pos" : "neg"}`}>{money2(snapshot.account.unrealized_pnl)}</div></div>
              <div><div className="stat-label">Realized P&L</div><div className={`stat-value ${snapshot.account.realized_pnl >= 0 ? "pos" : "neg"}`}>{money2(snapshot.account.realized_pnl)}</div></div>
              <div><div className="stat-label">Init Margin</div><div className="stat-value">{money(snapshot.account.init_margin)}</div></div>
              <div><div className="stat-label">Maint Margin</div><div className="stat-value">{money(snapshot.account.maint_margin)}</div></div>
            </div>
          ) : (
            <p className="muted" style={{ marginTop: 12 }}>Connect IBKR to see account balances.</p>
          )}
        </section>
```
(If the exact grid wrapper element differs, place the `<section>` as a sibling of the existing
market/positions `<section className="card">` blocks so it joins the same card layout.)

- [ ] **Step 3: Build the frontend**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.js frontend/src/components/Dashboard.jsx
git commit -m "feat(ui): account summary card on the dashboard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Verify (suite + live + desktop rebuild)

- [ ] **Step 1: Full backend suite**

Run: `cd backend && ../.venv/bin/python -m pytest -q`
Expected: all tests pass (incl. the 3 new account tests).

- [ ] **Step 2: Live REST check (app must be running + connected)**

Run: `curl -s -m10 http://127.0.0.1:8765/api/account`
Expected: JSON `{"account": {"net_liquidation": <num>, ... "account": "DUO710257", "currency": "USD"}, "note": null}`.
(If the app isn't running: `pkill -f TradingSystem; open desktop/dist/TradingSystem.app` then
`curl -s -X POST http://127.0.0.1:8765/api/auth/connect` first.)

- [ ] **Step 3: Rebuild the desktop app so the card ships in the bundle**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system && pkill -f TradingSystem; ./desktop/build.sh`
Expected: build completes; `desktop/dist/TradingSystem.app` updated. (User reopens it to see
the Account card with live balances.)

- [ ] **Step 4: Final commit (if anything outstanding)**

```bash
git add -A && git commit -m "test: account info on dashboard verified" || echo "nothing to commit"
```
