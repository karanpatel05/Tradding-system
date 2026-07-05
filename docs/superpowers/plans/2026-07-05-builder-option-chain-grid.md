# Builder Option-Chain Grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the option chain in the Builder tab — all expiries as clickable DTE chips and, for the selected expiry, a quoted near-ATM grid (call mid / strike / put mid); tapping a price adds that option as a leg.

**Architecture:** A new read-only backend helper fetches a ±count strike window's call+put mids concurrently for one expiry (reusing `aoption_mid`); a new `GET /api/builder/chain-quotes` exposes it. On the frontend a focused `ChainGrid` component renders the chips + grid and calls back to add legs; `BuilderTab` mounts it.

**Tech Stack:** Python 3.14, FastAPI, async SQLAlchemy, ib_async (read-only); React + Vite.

## Global Constraints

- **Run Python from `backend/`** with `../.venv/bin/python`. Pytest style; async tests use `@pytest.mark.asyncio`. These tests use monkeypatch/fakes — no live broker, no DB.
- **Branch:** `builder-chain-grid` — commit there; do NOT create/switch branches.
- **No order transmission** anywhere in this feature — read-only IBKR (chain + quotes). Do NOT import `app.engine.execution` / `submit_combo_order`.
- **Spot "unavailable" = `spot is None` OR `spot <= 0`** — IBKR returns `-1` (not `None`) when there's no live underlying quote; never treat `-1` as a price.
- **Mids are `null` when unavailable** (deep OTM / market closed / nonexistent strike) → the UI shows `—`. (The `conId` guard in `aoption_mid` already makes nonexistent strikes return `None` rather than error.)
- `count` clamped to **1–25** at the endpoint.
- Canonical leg dict: `{action:"BUY"|"SELL", right:"C"|"P", strike:float, expiry:"YYYY-MM-DD", qty:int}`.
- Style: `from __future__ import annotations`, module docstrings.

---

### Task 1: `get_chain_quotes` (backend helper)

**Files:**
- Modify: `backend/app/options/chain.py` (add a `get_quotes` import + the `get_chain_quotes` function)
- Test: `backend/tests/test_builder_chain_quotes.py`

**Interfaces:**
- Consumes: `get_option_chain` (existing, same module), `LivePriceSource.aoption_mid`, `app.ibkr.market.get_quotes`.
- Produces: `async get_chain_quotes(underlying: str, expiry: str, count: int = 8) -> dict` →
  `{underlying, expiry, spot, atm, count, rows: [{strike, call_mid, put_mid}]}` (rows ascending by
  strike; mids may be `None`; `atm=None` and `rows=[]` when spot unavailable / no strikes).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_builder_chain_quotes.py
import pytest

from app.options import chain


def _mk(monkeypatch, *, spot, strikes):
    async def fake_quotes(syms):
        return {"QQQ": {"price": spot}}

    async def fake_chain(u):
        return {"underlying": "QQQ", "expirations": [], "strikes": [float(s) for s in strikes]}

    async def fake_mid(self, u, expiry, strike, right):
        if strike == 500.0 and right == "P":
            return None  # a null inside the window -> must survive to the row
        return round(strike / 100 + (0.1 if right == "C" else 0.2), 2)

    monkeypatch.setattr(chain, "get_quotes", fake_quotes)
    monkeypatch.setattr(chain, "get_option_chain", fake_chain)
    monkeypatch.setattr(chain.LivePriceSource, "aoption_mid", fake_mid)


@pytest.mark.asyncio
async def test_windows_around_atm_with_null(monkeypatch):
    _mk(monkeypatch, spot=501.2, strikes=range(460, 541, 5))
    out = await chain.get_chain_quotes("QQQ", "2026-07-17", count=3)
    assert out["spot"] == 501.2 and out["atm"] == 500.0
    assert [r["strike"] for r in out["rows"]] == [485.0, 490.0, 495.0, 500.0, 505.0, 510.0, 515.0]
    atm_row = next(r for r in out["rows"] if r["strike"] == 500.0)
    assert atm_row["call_mid"] is not None and atm_row["put_mid"] is None
    assert all("call_mid" in r and "put_mid" in r for r in out["rows"])


@pytest.mark.asyncio
async def test_unavailable_spot_returns_empty(monkeypatch):
    _mk(monkeypatch, spot=-1.0, strikes=[490, 495, 500])  # IBKR sentinel
    out = await chain.get_chain_quotes("QQQ", "2026-07-17", count=3)
    assert out["atm"] is None and out["rows"] == []


@pytest.mark.asyncio
async def test_window_clamps_at_chain_edge(monkeypatch):
    _mk(monkeypatch, spot=501.2, strikes=[490, 495, 500])  # ATM at the top edge
    out = await chain.get_chain_quotes("QQQ", "2026-07-17", count=3)
    assert [r["strike"] for r in out["rows"]] == [490.0, 495.0, 500.0]  # clamped, no error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_builder_chain_quotes.py -q`
Expected: FAIL — `AttributeError: module 'app.options.chain' has no attribute 'get_chain_quotes'` (and no `get_quotes` to patch).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/options/chain.py`, add to the imports (below `from app.ibkr.gateway import get_gateway`):

```python
from app.ibkr.market import get_quotes
```

Then append this function:

```python
async def get_chain_quotes(underlying: str, expiry: str, count: int = 8) -> dict:
    """Near-ATM quoted slice for ONE expiry: call+put mids for `count` strikes each side of the
    ATM strike, fetched concurrently. Read-only. Mids are None when unavailable; atm/rows are
    empty when the underlying has no usable spot (IBKR returns -1 with no live quote)."""
    q = await get_quotes([underlying])
    spot = q.get(underlying, {}).get("price")
    chain = await get_option_chain(underlying)
    strikes = chain["strikes"]
    base = {"underlying": underlying.upper(), "expiry": expiry, "spot": spot,
            "atm": None, "count": count, "rows": []}
    if not strikes or spot is None or spot <= 0:
        return base
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    window = strikes[max(0, i - count): i + count + 1]
    exp = date.fromisoformat(expiry)
    src = LivePriceSource()
    tasks = []
    for s in window:
        tasks.append(src.aoption_mid(underlying, exp, s, "C"))
        tasks.append(src.aoption_mid(underlying, exp, s, "P"))
    mids = await asyncio.gather(*tasks)
    rows = [{"strike": s, "call_mid": mids[2 * idx], "put_mid": mids[2 * idx + 1]}
            for idx, s in enumerate(window)]
    return {**base, "atm": atm, "rows": rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_builder_chain_quotes.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/options/chain.py backend/tests/test_builder_chain_quotes.py
git commit -m "feat(builder): get_chain_quotes — concurrent near-ATM call/put mids for one expiry"
```

---

### Task 2: `/chain-quotes` endpoint

**Files:**
- Modify: `backend/app/routers/builder.py` (add the route after the existing `chain` route, ~line 78)
- Test: `backend/tests/test_builder_api.py` (extend)

**Interfaces:**
- Consumes: `chain_mod.get_chain_quotes` (Task 1).
- Produces: `GET /api/builder/chain-quotes?underlying=&expiry=&count=8` → the `get_chain_quotes` dict; `count` clamped to `[1, 25]`. Handler name `chain_quotes`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_builder_api.py`:

```python
@pytest.mark.asyncio
async def test_chain_quotes_route_clamps_count(monkeypatch):
    from app.routers import builder
    seen = {}

    async def fake(u, e, c):
        seen["count"] = c
        return {"underlying": u, "expiry": e, "count": c, "rows": []}

    monkeypatch.setattr(builder.chain_mod, "get_chain_quotes", fake)
    await builder.chain_quotes("QQQ", "2026-07-17", count=999)
    assert seen["count"] == 25          # clamped high
    await builder.chain_quotes("QQQ", "2026-07-17", count=0)
    assert seen["count"] == 1           # clamped low
    out = await builder.chain_quotes("QQQ", "2026-07-17", count=8)
    assert seen["count"] == 8 and out["rows"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && rm -f _t_api.db && DATABASE_URL=sqlite+aiosqlite:///./_t_api.db ../.venv/bin/python -m pytest tests/test_builder_api.py::test_chain_quotes_route_clamps_count -q; rm -f _t_api.db`
Expected: FAIL — `AttributeError: module 'app.routers.builder' has no attribute 'chain_quotes'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/routers/builder.py`, immediately after the existing `chain` route function, add:

```python
@router.get("/chain-quotes")
async def chain_quotes(underlying: str, expiry: str, count: int = 8) -> dict:
    count = max(1, min(int(count), 25))  # bound the concurrent quote fan-out
    return await chain_mod.get_chain_quotes(underlying, expiry, count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && rm -f _t_api.db && DATABASE_URL=sqlite+aiosqlite:///./_t_api.db ../.venv/bin/python -m pytest tests/test_builder_api.py -q; rm -f _t_api.db`
Expected: PASS (all builder-api tests, including the new one).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/builder.py backend/tests/test_builder_api.py
git commit -m "feat(builder): GET /api/builder/chain-quotes (count clamped 1-25)"
```

---

### Task 3: `ChainGrid` component + API method

**Files:**
- Modify: `frontend/src/api.js` (add `builderChainQuotes` after `builderChain`)
- Create: `frontend/src/components/ChainGrid.jsx`

**Interfaces:**
- Consumes: `api.builderChainQuotes(underlying, expiry, count)`.
- Produces: default export `ChainGrid({ underlying, expirations, onAddLeg })`. It owns its own selected-expiry / count / grid / loading state and calls `onAddLeg({action:"BUY", right, strike, expiry, qty:1})` when a price cell is tapped.

- [ ] **Step 1: Add the API method**

In `frontend/src/api.js`, immediately after the `builderChain:` line, insert:

```javascript
  builderChainQuotes: (underlying, expiry, count) =>
    request(`/api/builder/chain-quotes?underlying=${encodeURIComponent(underlying)}&expiry=${expiry}&count=${count}`),
```

- [ ] **Step 2: Create the component**

```jsx
// frontend/src/components/ChainGrid.jsx
import { useState } from "react";
import { api } from "../api.js";

// Visible option chain: all expiries as DTE chips + a near-ATM call/strike/put grid for the
// selected expiry. Tapping a price calls onAddLeg to append a BUY leg (side edited upstream).

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const label = (iso) => { const [y, m, d] = iso.split("-"); return `${MON[+m - 1]}-${d}`; };
const dte = (iso) => Math.max(0, Math.round((new Date(iso + "T00:00:00") - new Date().setHours(0, 0, 0, 0)) / 86400000));
const mid = (n) => (n == null ? "—" : n.toFixed(2));

export default function ChainGrid({ underlying, expirations = [], onAddLeg }) {
  const [expiry, setExpiry] = useState(null);
  const [count, setCount] = useState(8);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = async (exp, cnt) => {
    setLoading(true); setErr(null); setExpiry(exp);
    try { setData(await api.builderChainQuotes(underlying, exp, cnt)); }
    catch (e) { setErr(e.message); setData(null); }
    finally { setLoading(false); }
  };

  if (!expirations.length) return null;

  return (
    <section className="card chain-card">
      <div className="row between center">
        <h2>Option chain — {underlying}{data?.spot > 0 && <span className="badge">spot {data.spot.toFixed(2)}</span>}</h2>
        <div className="row gap center">
          <span className="muted small">Strikes ±</span>
          <select className="input tiny" value={count}
            onChange={(e) => { const c = Number(e.target.value); setCount(c); if (expiry) load(expiry, c); }}>
            {[6, 8, 12, 20].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          {expiry && <button className="btn ghost tiny" onClick={() => load(expiry, count)}>↻ Refresh</button>}
        </div>
      </div>

      <div className="muted small" style={{ marginTop: 10 }}>All expiries ({expirations.length}) — tap one to load quotes</div>
      <div className="chips">
        {expirations.map((x) => (
          <span key={x} className={`chip ${x === expiry ? "active" : ""}`} onClick={() => load(x, count)}>
            {label(x)} <span className="dte">{dte(x)}d</span>
          </span>
        ))}
      </div>

      {loading && <div className="muted">Loading quotes…</div>}
      {err && <div className="banner error">{err}</div>}
      {!loading && data && data.rows.length === 0 && expiry && (
        <div className="muted">No quotes (market closed or unavailable).</div>
      )}
      {!loading && data && data.rows.length > 0 && (
        <div className="table-scroll">
          <table className="table chain-grid">
            <thead><tr><th className="callh">Call mid</th><th className="strikeh">Strike</th><th className="puth">Put mid</th></tr></thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.strike} className={r.strike === data.atm ? "atm" : ""}>
                  <td className={`price call ${r.call_mid == null ? "muted" : ""}`}
                    onClick={() => r.call_mid != null && onAddLeg({ action: "BUY", right: "C", strike: r.strike, expiry, qty: 1 })}>
                    {mid(r.call_mid)}
                  </td>
                  <td className="strike num">{r.strike}</td>
                  <td className={`price put ${r.put_mid == null ? "muted" : ""}`}
                    onClick={() => r.put_mid != null && onAddLeg({ action: "BUY", right: "P", strike: r.strike, expiry, qty: 1 })}>
                    {mid(r.put_mid)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="hint muted small">Tap a call or put price to add it as a leg. “—” = no quote.</div>
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 3: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...` (no error).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.js frontend/src/components/ChainGrid.jsx
git commit -m "feat(builder): ChainGrid component (expiry chips + near-ATM quoted grid)"
```

---

### Task 4: Mount ChainGrid in BuilderTab + styles

**Files:**
- Modify: `frontend/src/components/BuilderTab.jsx` (import + add-leg handler + render)
- Modify: `frontend/src/styles.css` (append chain-grid styles)

**Interfaces:**
- Consumes: `ChainGrid` (Task 3); the existing `setLegs`, `invalidateQuote`, `chain`, `underlying` in `BuilderTab`.

- [ ] **Step 1: Wire BuilderTab**

In `frontend/src/components/BuilderTab.jsx`:
- Add the import after the `PayoffChart` import:
```javascript
import ChainGrid from "./ChainGrid.jsx";
```
- Add an add-leg handler next to the other leg mutators (right after the `removeLeg` definition):
```javascript
  const addLegFromChain = (leg) => { setLegs((ls) => [...ls, { ...leg, mid: null }]); invalidateQuote(); };
```
- Render the chain card. Immediately AFTER the first `</section>` (the "build" card that ends right after the `Fetch quotes` row), insert:
```javascript
      <ChainGrid underlying={underlying} expirations={chain.expirations} onAddLeg={addLegFromChain} />
```

- [ ] **Step 2: Append styles**

Append to `frontend/src/styles.css`:

```css
/* ===================== option chain grid (builder) ===================== */
.chain-card .badge{display:inline-block;background:var(--surface-3);color:var(--muted);border-radius:6px;padding:1px 7px;font-size:11px;font-family:var(--num);margin-left:8px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 6px}
.chip{padding:7px 12px;border-radius:20px;font-size:12.5px;cursor:pointer;background:var(--surface-2);border:1px solid var(--border);color:var(--text);white-space:nowrap}
.chip .dte{color:var(--faint);margin-left:6px;font-family:var(--num)}
.chip.active{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-bright)}
.chip.active .dte{color:var(--accent-bright)}
.chain-grid td{text-align:center;font-family:var(--num);font-variant-numeric:tabular-nums}
.chain-grid th.callh,.chain-grid th.puth,.chain-grid th.strikeh{text-align:center}
.chain-grid th.callh{color:var(--green)} .chain-grid th.puth{color:var(--red)}
.chain-grid .call{color:var(--green)} .chain-grid .put{color:var(--red)} .chain-grid .strike{color:var(--text)}
.chain-grid .price{cursor:pointer;border-radius:7px}
.chain-grid .price.muted{cursor:default;color:var(--faint)}
.chain-grid .price:not(.muted):hover{background:var(--accent-soft);color:var(--accent-bright)}
.chain-grid tr.atm td{background:rgba(99,102,241,.07)}
.chain-grid tr.atm .strike{color:var(--accent-bright);font-weight:700}
```

- [ ] **Step 3: Verify the build compiles**

Run: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"`
Expected: `✓ built in ...`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/BuilderTab.jsx frontend/src/styles.css
git commit -m "feat(builder): mount ChainGrid in the Builder tab + styles"
```

---

### Task 5: Full check + live verification

**Files:** none (verification only).

- [ ] **Step 1: Backend suite + import check**

Run:
```bash
cd backend
../.venv/bin/python -c "import app.main; print('main imports OK')"
rm -f _t_cg.db
DATABASE_URL=sqlite+aiosqlite:///./_t_cg.db ../.venv/bin/python -m pytest \
  tests/test_builder_chain_quotes.py tests/test_builder_api.py tests/test_builder_chain.py \
  tests/test_option_mid_unresolved.py -q
rm -f _t_cg.db
```
Expected: `main imports OK`; all pass.

- [ ] **Step 2: Rebuild frontend + restart + smoke the endpoint**

Run:
```bash
cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep "built in"
cd .. && launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
for i in $(seq 1 40); do curl -s -m3 http://127.0.0.1:8765/health 2>/dev/null | grep -q '"status":"ok"' && break; sleep 1; done
# pick a real expiry from the chain, then hit chain-quotes
EXP=$(curl -s -m20 "http://127.0.0.1:8765/api/builder/chain?underlying=QQQ" | python3 -c "import sys,json;print(json.load(sys.stdin)['expirations'][3])")
curl -s -m40 "http://127.0.0.1:8765/api/builder/chain-quotes?underlying=QQQ&expiry=$EXP&count=6" | python3 -m json.tool | head -30
```
Expected: HTTP 200 JSON with `atm`, `rows` (call_mid/put_mid may be `null` if the market is closed — that's fine; the endpoint must not 500).

- [ ] **Step 3: Manual (in the running app)**

Open **Builder** (`http://127.0.0.1:8765` or the iPad at `http://192.168.2.252:8765`): set underlying QQQ → **Load chain** → the **Option chain** card shows all expiry chips; tap one → the grid loads (call/strike/put, ATM row highlighted; `—` where no quote). Tap a call price → a `BUY Call` leg appears in the Legs table. Change ±count and Refresh. Confirm no error banner and no 500 in `~/Library/Logs/trading-system.err.log`.

- [ ] **Step 4: Final commit (only if manual tweaks were needed)**

```bash
git add -A && git commit -m "test(builder): verify chain grid end-to-end" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** `get_chain_quotes` incl. spot≤0 handling + concurrent mids (T1); `/chain-quotes` endpoint + count clamp (T2); `builderChainQuotes` + `ChainGrid` chips/grid/ATM-highlight/tap-to-add/refresh/loading (T3); mount + styles (T4); tests + live verify incl. null mids (T1/T2/T5). All covered.
- **No-transmit:** `ChainGrid`/`chain.py`/router are read-only; nothing imports execution.
- **Type consistency:** endpoint→`get_chain_quotes` args `(underlying, expiry, count)`; response `{spot, atm, rows:[{strike, call_mid, put_mid}]}` consumed verbatim by `ChainGrid`; `onAddLeg` emits the canonical leg dict `BuilderTab` already handles.
- **Placeholder scan:** none.
