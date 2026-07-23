# Builder Trade-Tracking View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Build | Trades sub-view to the Options Builder: an aggregate P&L summary plus a per-saved-trade payoff graph and net greeks.

**Architecture:** Pure frontend. A unit-tested `summarizeTrades()` computes portfolio totals from the trades list the frontend already loads. A new `BuilderTrades.jsx` renders the summary tiles, the saved-trades list (moved out of `BuilderTab`), and an inline payoff detail per trade — reusing `PayoffChart`, `GreeksPanel`, and the existing `builderTrade`/`builderSpot`/`builderGreeks`/`builderCloseTrade` endpoints. `BuilderTab` gains a view toggle. No backend changes.

**Tech Stack:** React 18 + Vite; recharts (via existing `PayoffChart`). Frontend tests are plain-node `.mjs` files (no runner), run with `node <path>`. Component changes are verified with `npm run build`.

## Global Constraints

- No backend changes. Reuse only existing endpoints: `api.builderTrades()`, `api.builderTrade(id)`, `api.builderSpot(underlying)` (returns `{price, ...}`), `api.builderGreeks({legs, spot, iv})` (returns `{net, legs}`), `api.builderCloseTrade(id)`.
- Trade record fields available: `id`, `underlying`, `legs` (`[{action,right,strike,expiry,qty}]`), `iv`, `status` (`"open"|"closed"`), `pnl` (closed), `last_pnl` / `last_pnl_pct` (open), `entry_time`, `exit_reason`.
- Saved-trade detail (`builderTrade(id)`) returns: `curve`, `eval_date`, `max_profit`, `max_loss`, `breakevens` (plus the serialized fields).
- `PayoffChart` props: `curve, breakevens=[], strikes=[], spot=null, height=380`. `GreeksPanel` props: `{ greeks }` (reads `greeks.net` with keys `delta/gamma/theta/vega`).
- Dollar values scale by nothing here — the backend already returns per-contract-scaled `pnl`/`last_pnl` and per-1x curve; display as-is.
- The frontend is `"type": "module"`; `.js` modules use ESM `export`. Run tests from repo root: `node frontend/src/components/builder/tradesSummary.test.mjs`.
- Commit after each task. Work on branch `builder-trades-tracking` (already created).

---

### Task 1: `summarizeTrades` pure function

**Files:**
- Create: `frontend/src/components/builder/tradesSummary.js`
- Test: `frontend/src/components/builder/tradesSummary.test.mjs`

**Interfaces:**
- Produces: `summarizeTrades(trades) -> { net, realized, unrealized, openCount, closedCount, winRate }`. `realized` = Σ closed `pnl`; `unrealized` = Σ open `last_pnl`; `net` = realized + unrealized; `winRate` = closed winners (`pnl > 0`) ÷ `closedCount`, or `null` when `closedCount === 0`. Null-safe: missing `pnl`/`last_pnl` count as 0; `null`/`undefined` input → all zeros.

- [ ] **Step 1: Write the failing test**

```javascript
// frontend/src/components/builder/tradesSummary.test.mjs
// Plain-node test (no runner). Run: node frontend/src/components/builder/tradesSummary.test.mjs
import { summarizeTrades } from "./tradesSummary.js";

const eq = (a, b, msg) => {
  const ok = b === null ? a === null : Math.abs(a - b) < 1e-9;
  if (!ok) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};

// Empty + nullish input → all zeros, winRate null.
for (const input of [[], null, undefined]) {
  const s = summarizeTrades(input);
  eq(s.net, 0, "empty net"); eq(s.realized, 0, "empty realized");
  eq(s.unrealized, 0, "empty unrealized"); eq(s.openCount, 0, "empty open");
  eq(s.closedCount, 0, "empty closed"); eq(s.winRate, null, "empty winRate");
}

// Mixed open/closed, one open trade unvalued (last_pnl null → counts as 0).
const s = summarizeTrades([
  { status: "closed", pnl: 100 },
  { status: "closed", pnl: -40 },
  { status: "open", last_pnl: 25 },
  { status: "open", last_pnl: null },
]);
eq(s.realized, 60, "mixed realized");
eq(s.unrealized, 25, "mixed unrealized");
eq(s.net, 85, "mixed net");
eq(s.openCount, 2, "mixed openCount");
eq(s.closedCount, 2, "mixed closedCount");
eq(s.winRate, 0.5, "mixed winRate");

// Win rate all winners.
eq(summarizeTrades([{ status: "closed", pnl: 5 }, { status: "closed", pnl: 10 }]).winRate, 1, "all winners");

console.log("PASS: summarizeTrades");
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node frontend/src/components/builder/tradesSummary.test.mjs`
Expected: FAIL — `ERR_MODULE_NOT_FOUND` (tradesSummary.js does not exist).

- [ ] **Step 3: Write minimal implementation**

```javascript
// frontend/src/components/builder/tradesSummary.js
// Portfolio totals across saved Builder trades. Pure + null-safe: the caller feeds
// the raw /api/builder/trades list (open trades carry last_pnl from the monitor;
// closed trades carry final pnl).
export function summarizeTrades(trades) {
  const list = Array.isArray(trades) ? trades : [];
  let realized = 0, unrealized = 0, openCount = 0, closedCount = 0, winners = 0;
  for (const t of list) {
    if (t.status === "closed") {
      closedCount += 1;
      const p = t.pnl || 0;
      realized += p;
      if (p > 0) winners += 1;
    } else {
      openCount += 1;
      unrealized += t.last_pnl || 0;
    }
  }
  return {
    net: realized + unrealized,
    realized,
    unrealized,
    openCount,
    closedCount,
    winRate: closedCount === 0 ? null : winners / closedCount,
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node frontend/src/components/builder/tradesSummary.test.mjs`
Expected: `PASS: summarizeTrades` (exit 0).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/builder/tradesSummary.js frontend/src/components/builder/tradesSummary.test.mjs
git commit -m "feat(builder): summarizeTrades portfolio P&L helper"
```

---

### Task 2: `BuilderTrades` component

**Files:**
- Create: `frontend/src/components/BuilderTrades.jsx`
- Modify: `frontend/src/styles.css` (append a small block at the end)

**Interfaces:**
- Consumes: `summarizeTrades` (Task 1); `api.builderTrades/builderTrade/builderSpot/builderGreeks/builderCloseTrade`; `PayoffChart`; `GreeksPanel`.
- Produces: default-exported `<BuilderTrades />` (no props). Mounted by Task 3.

- [ ] **Step 1: Create the component**

```jsx
// frontend/src/components/BuilderTrades.jsx
import { useEffect, useState } from "react";
import { api } from "../api.js";
import PayoffChart from "./PayoffChart.jsx";
import GreeksPanel from "./builder/GreeksPanel.jsx";
import { summarizeTrades } from "./builder/tradesSummary.js";

const money = (n) => (n == null ? "—" :
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pctv = (n) => (n == null ? "—" : `${(n * 100).toFixed(1)}%`);

// Trades tab: portfolio P&L summary + saved-trade list; clicking a trade expands its
// payoff diagram + net greeks. All read-only; paper trades only.
export default function BuilderTrades() {
  const [trades, setTrades] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);      // {full, spot, greeks} for selectedId
  const [detailErr, setDetailErr] = useState(false);

  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades || [])).catch(() => {});
  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);

  // Fetch payoff + live spot + net greeks for the selected trade (immutable per trade,
  // so keyed on selectedId only; live P&L in the row re-renders from the poll).
  useEffect(() => {
    if (selectedId == null) { setDetail(null); setDetailErr(false); return; }
    const t = trades.find((x) => x.id === selectedId);
    if (!t) return;
    let cancelled = false;
    setDetail(null); setDetailErr(false);
    api.builderTrade(selectedId).then(async (full) => {
      const spotResp = await api.builderSpot(t.underlying).catch(() => null);
      const spot = spotResp?.price ?? null;
      const greeks = spot != null
        ? await api.builderGreeks({ legs: t.legs, spot, iv: t.iv }).catch(() => null)
        : null;
      if (!cancelled) setDetail({ full, spot, greeks });
    }).catch(() => { if (!cancelled) setDetailErr(true); });
    return () => { cancelled = true; };
  }, [selectedId]);  // eslint-disable-line react-hooks/exhaustive-deps

  const closeTrade = async (id, e) => {
    e.stopPropagation();
    await api.builderCloseTrade(id).catch(() => {});
    loadTrades();
  };

  const s = summarizeTrades(trades);

  return (
    <div className="builder-trades">
      <section className="card">
        <h2>Portfolio P&amp;L</h2>
        <div className="deploy-grid">
          <div><div className="stat-label">Net P&amp;L</div>
            <div className={`stat-value ${s.net >= 0 ? "pos" : "neg"}`}>{money(s.net)}</div></div>
          <div><div className="stat-label">Realized</div>
            <div className={`stat-value ${s.realized >= 0 ? "pos" : "neg"}`}>{money(s.realized)}</div></div>
          <div><div className="stat-label">Unrealized</div>
            <div className={`stat-value ${s.unrealized >= 0 ? "pos" : "neg"}`}>{money(s.unrealized)}</div></div>
          <div><div className="stat-label">Open / Closed</div>
            <div className="stat-value">{s.openCount} / {s.closedCount}</div></div>
          <div><div className="stat-label">Win rate</div>
            <div className="stat-value">{pctv(s.winRate)}</div></div>
        </div>
      </section>

      <section className="card">
        <h2>Trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="trade-cards">
            {trades.map((t) => {
              const v = t.status === "closed" ? t.pnl : t.last_pnl;
              const isSel = t.id === selectedId;
              return (
                <div className={`trade-card ${isSel ? "selected" : ""}`} key={t.id}
                  style={{ cursor: "pointer" }}
                  onClick={() => setSelectedId(isSel ? null : t.id)}>
                  <div className="trade-head">
                    <b>{t.underlying}</b>
                    <span className="muted small">{t.legs.length} legs · {t.entry_time?.slice(0, 10) || "—"}</span>
                  </div>
                  <div className={`trade-pnl num ${(v ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {v == null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`}
                    {t.last_pnl_pct != null && t.status === "open" &&
                      <span className="muted small"> {(t.last_pnl_pct * 100).toFixed(1)}%</span>}
                  </div>
                  <div className="row between center">
                    <span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>
                      {t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span>
                    {t.status === "open" &&
                      <button className="btn ghost tiny" onClick={(e) => closeTrade(t.id, e)}>Close</button>}
                  </div>
                  {isSel && (
                    <div className="trade-detail" onClick={(e) => e.stopPropagation()}>
                      {detailErr ? <div className="muted">Couldn't load payoff.</div>
                        : !detail ? <div className="muted">Loading payoff…</div>
                        : (
                          <>
                            <PayoffChart curve={detail.full.curve}
                              breakevens={detail.full.breakevens || []}
                              strikes={t.legs.filter((l) => l.right !== "S").map((l) => l.strike)}
                              spot={detail.spot} height={300} />
                            <div className="deploy-grid" style={{ marginTop: 10 }}>
                              <div><div className="stat-label">Breakevens</div>
                                <div className="stat-value">
                                  {(detail.full.breakevens || []).map((b) => `$${b.toFixed(2)}`).join(" · ") || "—"}</div></div>
                              <div><div className="stat-label">Max profit</div>
                                <div className="stat-value pos">{money(detail.full.max_profit)}</div></div>
                              <div><div className="stat-label">Max loss</div>
                                <div className="stat-value neg">{money(detail.full.max_loss)}</div></div>
                              <div><div className="stat-label">Live P&amp;L</div>
                                <div className={`stat-value ${(v ?? 0) >= 0 ? "pos" : "neg"}`}>{money(v)}</div></div>
                            </div>
                            {detail.greeks && <GreeksPanel greeks={detail.greeks} />}
                          </>
                        )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 2: Append the CSS block**

Append to the end of `frontend/src/styles.css`:

```css
/* Builder Trades — selected card + inline payoff detail */
.trade-card.selected { outline: 1px solid var(--accent, #6366f1); }
.trade-detail {
  grid-column: 1 / -1;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
```

- [ ] **Step 3: Build to verify it compiles**

Run: `cd frontend && npm run build`
Expected: `✓ built` with no errors (the pre-existing "chunk larger than 500 kB" warning is fine).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/BuilderTrades.jsx frontend/src/styles.css
git commit -m "feat(builder): BuilderTrades view — portfolio P&L + per-trade payoff"
```

---

### Task 3: Wire the Build | Trades toggle into `BuilderTab`

**Files:**
- Modify: `frontend/src/components/BuilderTab.jsx`

**Interfaces:**
- Consumes: `<BuilderTrades />` (Task 2).

- [ ] **Step 1: Add the import**

In `frontend/src/components/BuilderTab.jsx`, add after the `PayoffChart` import line (line 4):

```javascript
import BuilderTrades from "./BuilderTrades.jsx";
```

- [ ] **Step 2: Add the `view` state; remove the trades state**

Replace this line (currently line 28):

```javascript
  const [trades, setTrades] = useState([]);
```

with:

```javascript
  const [view, setView] = useState("build");  // "build" | "trades"
```

- [ ] **Step 3: Remove the trades loader + poll (now owned by BuilderTrades)**

Delete these three lines (currently 79, 81, 82):

```javascript
  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades)).catch(() => {});

  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);
```

- [ ] **Step 4: On save, switch to the Trades view instead of reloading the list**

Replace (currently line 209):

```javascript
      setMsg("Saved as a tracked paper trade.");
      loadTrades();
```

with:

```javascript
      setMsg("Saved as a tracked paper trade.");
      setView("trades");
```

- [ ] **Step 5: Remove the now-unused `closeTrade` handler**

Delete this line (currently 213):

```javascript
  const closeTrade = async (id) => { await api.builderCloseTrade(id).catch(() => {}); loadTrades(); };
```

- [ ] **Step 6: Insert the toggle and open the Build-view wrapper**

Replace this transition (intro paragraph → first section):

```jsx
        tracked <b>paper</b> trade. Nothing is transmitted to the broker.</p>

      <section className="card">
```

with:

```jsx
        tracked <b>paper</b> trade. Nothing is transmitted to the broker.</p>

      <div className="seg-toggle" style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <button className={`btn ${view === "build" ? "primary" : "ghost"}`}
          onClick={() => setView("build")}>Build</button>
        <button className={`btn ${view === "trades" ? "primary" : "ghost"}`}
          onClick={() => setView("trades")}>Trades</button>
      </div>

      {view === "trades" ? <BuilderTrades /> : (
      <>

      <section className="card">
```

- [ ] **Step 7: Close the wrapper and delete the old Recent Trades section**

Replace this block (the Recent Trades `<section>` through the component's end):

```jsx
      <section className="card">
        <h2>Recent Trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="trade-cards">
            {trades.map((t) => {
              const v = t.status === "closed" ? t.pnl : t.last_pnl;
              return (
                <div className="trade-card" key={t.id}>
                  <div className="trade-head">
                    <b>{t.underlying}</b>
                    <span className="muted small">{t.legs.length} legs · {t.entry_time?.slice(0, 10) || "—"}</span>
                  </div>
                  <div className={`trade-pnl num ${(v ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {v == null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`}
                    {t.last_pnl_pct != null && t.status === "open" &&
                      <span className="muted small"> {(t.last_pnl_pct * 100).toFixed(1)}%</span>}
                  </div>
                  <div className="row between center">
                    <span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>{t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span>
                    {t.status === "open" && <button className="btn ghost tiny" onClick={() => closeTrade(t.id)}>Close</button>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
```

with:

```jsx
      </>
      )}
    </div>
  );
}
```

- [ ] **Step 8: Build to verify it compiles**

Run: `cd frontend && npm run build`
Expected: `✓ built` with no errors. (If the build complains about an unused `api` import, ignore — `api` is still used elsewhere in BuilderTab.)

- [ ] **Step 9: Commit**

```bash
git add frontend/src/components/BuilderTab.jsx
git commit -m "feat(builder): Build | Trades toggle; move saved trades into BuilderTrades"
```

---

## Self-Review

**Spec coverage:**
- Build | Trades sub-view toggle → Task 3 (Steps 2, 6, 7). ✓
- Aggregate P&L summary (net/realized/unrealized/counts/win rate) → Task 1 (`summarizeTrades`) + Task 2 (tiles). ✓
- Saved-trades list moved out of BuilderTab → Task 2 (list) + Task 3 (Step 7 removes old block; Steps 2,3,5 remove state). ✓
- Per-trade payoff detail (payoff + breakevens/max-P&L + live P&L + greeks) → Task 2 (detail panel; `builderTrade`+`builderSpot`+`builderGreeks`; `PayoffChart`+`GreeksPanel`). ✓
- Works for open and closed trades → Task 2 (`v = closed ? pnl : last_pnl`; curve renders from legs regardless of status). ✓
- Switch to Trades on save → Task 3 (Step 4). ✓
- No backend changes → confirmed; only existing endpoints used. ✓
- Independent error degradation → Task 2 (`.catch` per fetch; `detailErr` fallback). ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. ✓

**Type consistency:** `summarizeTrades` return keys (`net/realized/unrealized/openCount/closedCount/winRate`) are identical in Task 1's module, its test, and Task 2's consumer. `PayoffChart` props (`curve/breakevens/strikes/spot/height`) and `GreeksPanel` prop (`greeks` → reads `.net`) match their definitions. `builderSpot` → `.price`, `builderGreeks({legs,spot,iv})` → `{net}`, `builderTrade` → `{curve, breakevens, max_profit, max_loss}` all match the backend. ✓
