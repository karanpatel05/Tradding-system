# Builder Visual Option Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the Builder tab's option picking + payoff presentation into the tastytrade-style reference UI: month-grouped expiry tabs, strike ruler with C/P badge, stats row, gradient payoff chart, RANGE/IV sliders — as a visual layer over the existing legs/grid/save flow.

**Architecture:** Four new frontend components under `frontend/src/components/builder/` plus one pure stats module; `PayoffChart.jsx` upgraded in place; `BuilderTab.jsx` gains a selected-leg model and the new layout. Zero backend changes — chain/quote/payoff endpoints already provide everything (curve `pnl` is already dollars ×100).

**Tech Stack:** React 18, Recharts (already a dependency), plain CSS in `frontend/src/styles.css`. No test runner is configured in the frontend — the one logic module (`payoffStats.js`) is tested with a plain `node` script.

**Spec:** `docs/superpowers/specs/2026-07-10-builder-visual-chain-design.md`

## Global Constraints

- **No backend changes.** All data from existing endpoints: `GET /api/builder/chain` (`{expirations, strikes}`), `GET /api/builder/chain-quotes` (`{spot, atm, rows}`), `POST /api/builder/quote`, `POST /api/builder/payoff` (accepts `lo`/`hi`/`points`; returns `{curve: [{price, pnl}], breakevens, max_profit, max_loss, eval_date}` — `pnl` in dollars, ×100 already applied).
- Builder NEVER transmits orders (unchanged invariant; no new API calls beyond the existing ones).
- Any leg mutation must keep going through `setLeg`/`invalidateQuote` so a stale `net_debit` can never be saved (existing fix, must be preserved).
- No date scrubber; no "chance of profit" (spec non-goals).
- Every task ends with `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` printing `✓ built in …`.
- Frontend dir: `/Users/karanpatel/PycharmProjects/Tradding-system/frontend`.

## File Structure

```
frontend/src/components/builder/payoffStats.js        (new) pure stats: computeStats
frontend/src/components/builder/payoffStats.test.mjs  (new) node test script
frontend/src/components/builder/ExpiryTabs.jsx        (new) month-grouped expiry bar
frontend/src/components/builder/StrikeRuler.jsx       (new) strike ruler + badge
frontend/src/components/builder/StatsRow.jsx          (new) stats strip
frontend/src/components/PayoffChart.jsx               (modify) gradient split + markers
frontend/src/components/ChainGrid.jsx                 (modify) +onSpot callback (2 lines)
frontend/src/components/BuilderTab.jsx                (modify) selected-leg model + layout
frontend/src/styles.css                               (modify) new classes appended
```

---

### Task 1: `payoffStats.js` — pure curve statistics (+ node test)

**Files:**
- Create: `frontend/src/components/builder/payoffStats.js`
- Test: `frontend/src/components/builder/payoffStats.test.mjs`

**Interfaces:**
- Produces: `computeStats(curve, breakevens = [], spot = null) -> { maxProfit: number|"infinite"|null, maxLoss: number|"infinite"|null, breakevens: [{price, movePct: number|null}] }`. Curve pnl values are dollars (×100 upstream). Consumed by `StatsRow` (Task 4) and `BuilderTab` (Task 5).

- [ ] **Step 1: Write the failing test**

```javascript
// frontend/src/components/builder/payoffStats.test.mjs
// Plain-node test (no runner configured). Run: node frontend/src/components/builder/payoffStats.test.mjs
import { computeStats } from "./payoffStats.js";

const eq = (a, b, msg) => {
  const ok = typeof b === "number" ? Math.abs(a - b) < 1e-6 : a === b;
  if (!ok) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};

// Long call K=100, debit $2/share -> flat -$200 left, +$100/$1 slope right.
const longCall = [
  { price: 80, pnl: -200 }, { price: 90, pnl: -200 }, { price: 100, pnl: -200 },
  { price: 110, pnl: 800 }, { price: 120, pnl: 1800 },
];
let s = computeStats(longCall, [102], 100);
eq(s.maxProfit, "infinite", "long call maxProfit infinite");
eq(s.maxLoss, -200, "long call maxLoss = debit");
eq(s.breakevens[0].price, 102, "breakeven price");
eq(s.breakevens[0].movePct, 0.02, "breakeven movePct");

// Short call: mirrored -> bounded profit, infinite loss.
const shortCall = longCall.map((p) => ({ price: p.price, pnl: -p.pnl }));
s = computeStats(shortCall, [102], 100);
eq(s.maxProfit, 200, "short call maxProfit = credit");
eq(s.maxLoss, "infinite", "short call maxLoss infinite");

// Long put K=100, debit $3/share: left edge extrapolates to price 0 -> finite 9700.
const longPut = [
  { price: 60, pnl: 3700 }, { price: 80, pnl: 1700 },
  { price: 100, pnl: -300 }, { price: 120, pnl: -300 },
];
s = computeStats(longPut, [97], 100);
eq(s.maxProfit, 9700, "long put maxProfit via price-0 extrapolation");
eq(s.maxLoss, -300, "long put maxLoss = debit");

// Bounded both sides (calendar-ish hump): both finite, no extrapolation surprises.
const hump = [
  { price: 90, pnl: -100 }, { price: 95, pnl: 150 }, { price: 100, pnl: 400 },
  { price: 105, pnl: 150 }, { price: 110, pnl: -100 },
];
s = computeStats(hump, [93, 107], 100);
eq(s.maxProfit, 400, "hump maxProfit");
// left edge slope = (150-(-100))/5 = +50/$ -> extrapolation to 0 gives -100-50*90 = -4600
eq(s.maxLoss, -4600, "hump maxLoss includes price-0 extrapolation");
eq(s.breakevens.length, 2, "two breakevens");
eq(s.breakevens[0].movePct, -0.07, "downside movePct negative");

// Degenerate inputs never crash.
s = computeStats([], [], null);
eq(s.maxProfit, null, "empty curve -> null");
s = computeStats([{ price: 100, pnl: 0 }], [], null);
eq(s.maxProfit, null, "single point -> null");
s = computeStats(longCall, [102], null);
eq(s.breakevens[0].movePct, null, "no spot -> movePct null");

console.log("ALL PAYOFF-STATS TESTS PASSED");
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && node src/components/builder/payoffStats.test.mjs`
Expected: FAIL — cannot find module `./payoffStats.js`.

- [ ] **Step 3: Implement**

```javascript
// frontend/src/components/builder/payoffStats.js
// Pure payoff-curve statistics for the Builder stats row. Curve pnl is dollars (×100
// upstream). Right edge (price → ∞) can be genuinely unbounded; the left edge (price → 0)
// never is — it's extended by linear extrapolation to price 0 instead.
const EPS = 1.0; // $ per $1 of underlying (= $0.01/share on a ×100 curve)

export function computeStats(curve, breakevens = [], spot = null) {
  const bes = (breakevens || []).map((be) => ({
    price: be,
    movePct: spot ? (be - spot) / spot : null,
  }));
  if (!curve || curve.length < 2) return { maxProfit: null, maxLoss: null, breakevens: bes };

  const n = curve.length;
  const slope = (a, b) => (b.pnl - a.pnl) / (b.price - a.price || 1);
  const sR = slope(curve[n - 2], curve[n - 1]);
  const sL = slope(curve[0], curve[1]);
  // Left edge is always bounded: extend linearly down to price 0.
  const pnlAtZero = curve[0].pnl - sL * curve[0].price;
  const pnls = curve.map((p) => p.pnl).concat([pnlAtZero]);

  let maxProfit = Math.max(...pnls);
  let maxLoss = Math.min(...pnls);
  if (sR > EPS) maxProfit = "infinite";
  if (sR < -EPS) maxLoss = "infinite";
  return { maxProfit, maxLoss, breakevens: bes };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && node src/components/builder/payoffStats.test.mjs`
Expected: `ALL PAYOFF-STATS TESTS PASSED`.

- [ ] **Step 5: Build check + commit**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → `✓ built in …`
```bash
git add frontend/src/components/builder/payoffStats.js frontend/src/components/builder/payoffStats.test.mjs
git commit -m "feat(builder-ui): payoff curve stats with infinite detection (node-tested)"
```

---

### Task 2: `ExpiryTabs` — month-grouped expiry bar

**Files:**
- Create: `frontend/src/components/builder/ExpiryTabs.jsx`
- Modify: `frontend/src/styles.css` (append)

**Interfaces:**
- Produces: `<ExpiryTabs expirations={string[] ISO} value={string|null} onChange={(iso)=>{}} />`. Renders null when `expirations` is empty. Consumed by `BuilderTab` (Task 5).

- [ ] **Step 1: Implement the component**

```jsx
// frontend/src/components/builder/ExpiryTabs.jsx
import { useEffect, useMemo, useRef } from "react";

// Month-grouped expiry picker: month segments on top, day numbers underneath,
// selected day highlighted. Horizontal scroll; selected day auto-scrolled into view.
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function ExpiryTabs({ expirations = [], value, onChange }) {
  const ref = useRef(null);
  const groups = useMemo(() => {
    const out = [];
    const thisYear = new Date().getFullYear();
    let cur = null;
    for (const iso of expirations) {
      const [y, m] = iso.split("-").map(Number);
      const key = `${y}-${m}`;
      if (!cur || cur.key !== key) {
        cur = { key, label: `${MON[m - 1]}${y !== thisYear ? ` '${String(y).slice(2)}` : ""}`, days: [] };
        out.push(cur);
      }
      cur.days.push(iso);
    }
    return out;
  }, [expirations]);

  useEffect(() => {
    ref.current?.querySelector(".xt-day.active")
      ?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [value]);

  if (!expirations.length) return null;
  return (
    <div className="xtabs" ref={ref}>
      {groups.map((g) => (
        <div className="xt-month" key={g.key}>
          <div className={`xt-month-label ${g.days.includes(value) ? "active" : ""}`}>{g.label}</div>
          <div className="xt-days">
            {g.days.map((iso) => (
              <button key={iso} type="button"
                className={`xt-day ${iso === value ? "active" : ""}`}
                onClick={() => onChange(iso)}>
                {Number(iso.slice(8))}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Append the CSS**

Append to `frontend/src/styles.css`:

```css
/* --- Builder visual chain: expiry tabs --- */
.xtabs { display: flex; gap: 14px; overflow-x: auto; padding: 6px 2px 10px; scrollbar-width: thin; }
.xt-month { flex: 0 0 auto; }
.xt-month-label { text-align: center; font-size: 12px; color: var(--muted, #8b93a7);
  background: #1a2130; border-radius: 8px; padding: 3px 14px; margin-bottom: 6px; }
.xt-month-label.active { color: #e6e9f2; background: #232d42; }
.xt-days { display: flex; gap: 4px; justify-content: center; }
.xt-day { border: none; background: transparent; color: #aab2c5; font-size: 13px;
  padding: 4px 7px; border-radius: 7px; cursor: pointer; }
.xt-day:hover { background: #1e2636; color: #e6e9f2; }
.xt-day.active { background: #2563eb; color: #fff; font-weight: 600; }
```

- [ ] **Step 3: Build check + commit**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → `✓ built in …`
```bash
git add frontend/src/components/builder/ExpiryTabs.jsx frontend/src/styles.css
git commit -m "feat(builder-ui): month-grouped ExpiryTabs"
```

---

### Task 3: `StrikeRuler` — tick ruler with C/P badge

**Files:**
- Create: `frontend/src/components/builder/StrikeRuler.jsx`
- Modify: `frontend/src/styles.css` (append)

**Interfaces:**
- Produces: `<StrikeRuler strikes={number[]} value={number|null} right={"C"|"P"} action={"BUY"|"SELL"} spot={number|null} onChange={(strike)=>{}} />`. Renders null when `strikes` is empty. Click or drag selects the nearest strike (snapping). Consumed by `BuilderTab` (Task 5).

- [ ] **Step 1: Implement the component**

```jsx
// frontend/src/components/builder/StrikeRuler.jsx
import { useEffect, useMemo, useRef } from "react";

// Horizontal strike ruler: one tick per strike, labels every Nth, scrollable and
// auto-centered on the selection. Badge above the active tick shows e.g. "725C"
// (green = BUY leg, red = SELL). Click or drag (pointer capture) to select; snaps
// to real strikes. Thin marker at spot when known.
const TICK_W = 12;

export default function StrikeRuler({ strikes = [], value, right = "C", action = "BUY",
                                      spot = null, onChange }) {
  const wrap = useRef(null);

  const idx = useMemo(() => {
    if (!strikes.length) return -1;
    if (value != null) {
      const i = strikes.indexOf(value);
      if (i >= 0) return i;
    }
    const target = spot ?? strikes[Math.floor(strikes.length / 2)];
    let best = 0;
    strikes.forEach((s, i) => { if (Math.abs(s - target) < Math.abs(strikes[best] - target)) best = i; });
    return best;
  }, [strikes, value, spot]);

  const spotIdx = useMemo(() => {
    if (spot == null || !strikes.length) return -1;
    let best = 0;
    strikes.forEach((s, i) => { if (Math.abs(s - spot) < Math.abs(strikes[best] - spot)) best = i; });
    return best;
  }, [strikes, spot]);

  const labelEvery = Math.max(1, Math.round(strikes.length / 14));

  useEffect(() => {
    const el = wrap.current;
    if (el && idx >= 0) el.scrollLeft = idx * TICK_W - el.clientWidth / 2;
  }, [idx, strikes.length]);

  const pick = (clientX) => {
    const el = wrap.current;
    if (!el) return;
    const x = clientX - el.getBoundingClientRect().left + el.scrollLeft;
    const i = Math.max(0, Math.min(strikes.length - 1, Math.round(x / TICK_W - 0.5)));
    if (strikes[i] !== value) onChange(strikes[i]);
  };

  if (!strikes.length) return null;
  return (
    <div className="sruler" ref={wrap}
      onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); pick(e.clientX); }}
      onPointerMove={(e) => { if (e.buttons & 1) pick(e.clientX); }}>
      <div className="sruler-track" style={{ width: strikes.length * TICK_W }}>
        {idx >= 0 && (
          <div className={`sruler-badge ${action === "BUY" ? "buy" : "sell"}`}
            style={{ left: idx * TICK_W + TICK_W / 2 }}>
            {strikes[idx]}{right}
          </div>
        )}
        {spotIdx >= 0 && <div className="sruler-spot" style={{ left: spotIdx * TICK_W + TICK_W / 2 }} />}
        {strikes.map((s, i) => (
          <div key={s} className={`sruler-tick ${i === idx ? "active" : ""}`}
            style={{ left: i * TICK_W }}>
            <div className={`sruler-line ${i % labelEvery === 0 ? "major" : ""}`} />
            {i % labelEvery === 0 && <span className="sruler-label">{s}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Append the CSS**

Append to `frontend/src/styles.css`:

```css
/* --- Builder visual chain: strike ruler --- */
.sruler { overflow-x: auto; padding: 34px 0 4px; cursor: pointer; user-select: none;
  touch-action: pan-x; scrollbar-width: thin; }
.sruler-track { position: relative; height: 44px; }
.sruler-tick { position: absolute; top: 0; width: 12px; }
.sruler-line { width: 1px; height: 14px; margin: 0 auto; background: #39415a; }
.sruler-line.major { height: 22px; background: #556; }
.sruler-tick.active .sruler-line { background: #e6e9f2; height: 24px; }
.sruler-label { display: block; text-align: center; font-size: 10px; color: #8b93a7;
  margin-top: 3px; transform: translateX(-38%); white-space: nowrap; }
.sruler-badge { position: absolute; top: -30px; transform: translateX(-50%);
  padding: 3px 10px; border-radius: 7px; font-size: 12px; font-weight: 700; color: #08110a; }
.sruler-badge::after { content: ""; position: absolute; left: 50%; bottom: -5px;
  transform: translateX(-50%); border: 5px solid transparent; border-bottom: none; }
.sruler-badge.buy { background: #22c55e; }
.sruler-badge.buy::after { border-top-color: #22c55e; }
.sruler-badge.sell { background: #ef4444; color: #fff; }
.sruler-badge.sell::after { border-top-color: #ef4444; }
.sruler-spot { position: absolute; top: 0; width: 2px; height: 26px; background: #60a5fa;
  opacity: 0.7; }
```

- [ ] **Step 3: Build check + commit**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → `✓ built in …`
```bash
git add frontend/src/components/builder/StrikeRuler.jsx frontend/src/styles.css
git commit -m "feat(builder-ui): StrikeRuler with drag-snap and C/P badge"
```

---

### Task 4: `StatsRow` + gradient `PayoffChart`

**Files:**
- Create: `frontend/src/components/builder/StatsRow.jsx`
- Modify: `frontend/src/components/PayoffChart.jsx` (full replace, shown below)
- Modify: `frontend/src/styles.css` (append)

**Interfaces:**
- Consumes: `computeStats` (Task 1).
- Produces: `<StatsRow netDebit={number|null} stats={computeStats result|null} />` and `<PayoffChart curve breakevens strikes={number[]} spot={number|null} window={[lo,hi]|null} />` (old call sites pass only `curve`/`breakevens` and must keep working). Consumed by `BuilderTab` (Task 5).

- [ ] **Step 1: Implement StatsRow**

```jsx
// frontend/src/components/builder/StatsRow.jsx
// Screenshot-style stats strip: NET DEBIT / MAX LOSS / MAX PROFIT / BREAKEVEN.
// Values are dollars (curve is ×100 upstream); netDebit is per-share and scaled here.
const usd = (n) => `$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const Stat = ({ label, children, tone }) => (
  <div className="ostat">
    <div className="ostat-label">{label}</div>
    <div className={`ostat-value ${tone || ""}`}>{children}</div>
  </div>
);

export default function StatsRow({ netDebit, stats }) {
  const debit$ = netDebit == null ? null : netDebit * 100;
  const fmt = (v, tone) => v == null ? "—" : v === "infinite"
    ? <span className={tone}>Infinite</span> : usd(v);
  const bes = stats?.breakevens || [];
  return (
    <div className="ostats">
      <Stat label={netDebit != null && netDebit < 0 ? "NET CREDIT:" : "NET DEBIT:"}>
        {debit$ == null ? "—" : `🪙 ${usd(debit$)}`}
      </Stat>
      <Stat label="MAX LOSS:" tone="neg">
        {fmt(stats?.maxLoss, "neg")}
      </Stat>
      <Stat label="MAX PROFIT:" tone="pos">
        {fmt(stats?.maxProfit, "pos")}
      </Stat>
      <Stat label="BREAKEVEN:">
        {bes.length === 0 ? "—" : bes.map((b, i) => (
          <span key={i}>
            {i > 0 && " / "}
            {bes.length === 1 && b.movePct != null && (b.movePct >= 0 ? "Above " : "Below ")}
            <b>{usd(b.price)}</b>
            {b.movePct != null && <span className="muted small"> ({b.movePct >= 0 ? "+" : ""}{(b.movePct * 100).toFixed(1)}%)</span>}
          </span>
        ))}
      </Stat>
    </div>
  );
}
```

- [ ] **Step 2: Replace PayoffChart with the gradient version**

Full new content of `frontend/src/components/PayoffChart.jsx`:

```jsx
import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

// Payoff diagram: P&L (dollars) across underlying price at the chosen evaluation date.
// Green above zero / red below via a gradient split at the zero crossing; dotted lines
// at leg strikes, blue line + label at each breakeven, thin marker at spot.
const splitOffset = (data) => {
  const max = Math.max(...data.map((d) => d.pnl));
  const min = Math.min(...data.map((d) => d.pnl));
  if (max <= 0) return 0;
  if (min >= 0) return 1;
  return max / (max - min);
};

export default function PayoffChart({ curve, breakevens = [], strikes = [], spot = null, window = null }) {
  if (!curve || curve.length === 0) return <div className="muted">No payoff data.</div>;
  const data = window ? curve.filter((p) => p.price >= window[0] && p.price <= window[1]) : curve;
  if (data.length < 2) return <div className="muted">No payoff data in range.</div>;
  const off = splitOffset(data);
  const inWin = (x) => x >= data[0].price && x <= data[data.length - 1].price;
  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={data} margin={{ top: 18, right: 12, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset={0} stopColor="#22c55e" stopOpacity={0.45} />
            <stop offset={off} stopColor="#22c55e" stopOpacity={0.04} />
            <stop offset={off} stopColor="#ef4444" stopOpacity={0.06} />
            <stop offset={1} stopColor="#ef4444" stopOpacity={0.5} />
          </linearGradient>
          <linearGradient id="pnlStroke" x1="0" y1="0" x2="0" y2="1">
            <stop offset={0} stopColor="#22c55e" />
            <stop offset={off} stopColor="#22c55e" />
            <stop offset={off} stopColor="#ef4444" />
            <stop offset={1} stopColor="#ef4444" />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#243" vertical={false} />
        <XAxis dataKey="price" type="number" domain={["dataMin", "dataMax"]}
          tick={{ fontSize: 10 }} minTickGap={40}
          tickFormatter={(v) => `$${Math.round(v)}`} />
        <YAxis tick={{ fontSize: 10 }} tickFormatter={(v) => `$${v.toLocaleString()}`} width={64} />
        <Tooltip
          contentStyle={{ background: "#131722", border: "1px solid #242c3a", borderRadius: 8 }}
          formatter={(v) => [`$${v.toLocaleString()}`, "P&L"]}
          labelFormatter={(l) => `Underlying $${l}`}
        />
        <ReferenceLine y={0} stroke="#5c6677" />
        {strikes.filter(inWin).map((k) => (
          <ReferenceLine key={`k${k}`} x={k} stroke="#8b93a7" strokeDasharray="3 4" />
        ))}
        {spot != null && inWin(spot) && (
          <ReferenceLine x={spot} stroke="#e6e9f2" strokeOpacity={0.35} />
        )}
        {breakevens.filter(inWin).map((be) => (
          <ReferenceLine key={`be${be}`} x={be} stroke="#60a5fa"
            label={{ value: `$${be}`, position: "top", fill: "#60a5fa", fontSize: 11 }} />
        ))}
        <Area type="monotone" dataKey="pnl" stroke="url(#pnlStroke)" strokeWidth={2}
          fill="url(#pnlFill)" isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
```

- [ ] **Step 3: Append the CSS**

Append to `frontend/src/styles.css`:

```css
/* --- Builder visual chain: stats strip --- */
.ostats { display: flex; flex-wrap: wrap; gap: 18px 34px; padding: 12px 4px;
  border-top: 1px solid #1e2636; border-bottom: 1px solid #1e2636; margin: 10px 0; }
.ostat-label { font-size: 11px; letter-spacing: 0.06em; color: #8b93a7; margin-bottom: 3px; }
.ostat-value { font-size: 15px; font-weight: 600; color: #e6e9f2; }
.ostat-value.pos, .ostat-value .pos { color: #22c55e; }
.ostat-value.neg, .ostat-value .neg { color: #ef4444; }
```

- [ ] **Step 4: Build check + commit**

Run: `cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → `✓ built in …`
```bash
git add frontend/src/components/builder/StatsRow.jsx frontend/src/components/PayoffChart.jsx frontend/src/styles.css
git commit -m "feat(builder-ui): StatsRow + gradient split PayoffChart"
```

---

### Task 5: BuilderTab integration — selected-leg model, layout, sliders

**Files:**
- Modify: `frontend/src/components/BuilderTab.jsx`
- Modify: `frontend/src/components/ChainGrid.jsx` (add `onSpot` callback)
- Modify: `frontend/src/styles.css` (append)

**Interfaces:**
- Consumes: `ExpiryTabs`, `StrikeRuler`, `StatsRow`, `computeStats`, upgraded `PayoffChart` (Tasks 1–4).
- Produces: the final Builder layout. `ChainGrid` gains optional prop `onSpot(spotNumber)`.

- [ ] **Step 1: ChainGrid onSpot (2-line change)**

In `frontend/src/components/ChainGrid.jsx`, change the signature and `load`:
```jsx
export default function ChainGrid({ underlying, expirations = [], onAddLeg, onSpot }) {
```
and inside `load`, after `setData(...)` succeeds, report spot up (replace the try body):
```jsx
    try {
      const d = await api.builderChainQuotes(underlying, exp, cnt);
      setData(d);
      if (d?.spot > 0) onSpot?.(d.spot);
    }
```

- [ ] **Step 2: Rework BuilderTab**

Apply these changes to `frontend/src/components/BuilderTab.jsx`:

Imports (add):
```jsx
import ExpiryTabs from "./builder/ExpiryTabs.jsx";
import StrikeRuler from "./builder/StrikeRuler.jsx";
import StatsRow from "./builder/StatsRow.jsx";
import { computeStats } from "./builder/payoffStats.js";
```

New state (after the existing `useState` block):
```jsx
  const [selectedLeg, setSelectedLeg] = useState(null); // index into legs
  const [spot, setSpot] = useState(null);
  const [rangePct, setRangePct] = useState(8);          // chart x-window ±%
```
Change the IV default so the slider starts sane: keep `const [iv, setIv] = useState(0.25);`.

Reset spot when the underlying changes (add to the underlying input's onChange):
```jsx
          <input className="input narrow" value={underlying}
            onChange={(e) => { setUnderlying(e.target.value.toUpperCase()); setSpot(null); }} placeholder="Underlying" />
```

Selected-leg helpers (after `removeLeg`):
```jsx
  // The expiry tabs + strike ruler edit the SELECTED leg. With no legs yet, first
  // interaction creates leg 0 (BUY CALL at the picked value) and selects it.
  const atmStrike = () => {
    const ks = chain.strikes;
    if (!ks.length) return 0;
    if (spot == null) return ks[Math.floor(ks.length / 2)];
    return ks.reduce((a, b) => (Math.abs(b - spot) < Math.abs(a - spot) ? b : a));
  };
  const pickExpiry = (iso) => {
    if (selectedLeg == null || !legs[selectedLeg]) {
      setLegs((ls) => [...ls, { ...emptyLeg(iso, atmStrike()) }]);
      setSelectedLeg(legs.length);
      invalidateQuote();
    } else setLeg(selectedLeg, { expiry: iso });
  };
  const pickStrike = (k) => {
    if (selectedLeg == null || !legs[selectedLeg]) {
      setLegs((ls) => [...ls, { ...emptyLeg(chain.expirations[0] || "", k) }]);
      setSelectedLeg(legs.length);
      invalidateQuote();
    } else setLeg(selectedLeg, { strike: k });
  };
```
Update `addLeg` / `removeLeg` / `addLegFromChain` to manage selection:
```jsx
  const addLeg = () => { setLegs((ls) => [...ls, emptyLeg(chain.expirations[0] || "", atmStrike())]); setSelectedLeg(legs.length); invalidateQuote(); };
  const removeLeg = (i) => {
    setLegs((ls) => ls.filter((_, j) => j !== i));
    setSelectedLeg((sel) => (sel == null ? null : sel === i ? (i > 0 ? i - 1 : null) : sel > i ? sel - 1 : sel));
    invalidateQuote();
  };
  const addLegFromChain = (leg) => { setLegs((ls) => [...ls, { ...leg, mid: null }]); setSelectedLeg(legs.length); invalidateQuote(); };
```

Preview with a wide window when spot is known (so the RANGE slider has data to clip; curve is fetched once, sliced client-side):
```jsx
  const preview = async () => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try {
      const body = { legs, net_debit: netDebit, iv: Number(iv) };
      if (spot != null) { body.lo = spot * 0.5; body.hi = spot * 1.5; body.points = 161; }
      setPayoff(await api.builderPayoff(body));
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
```

Derived values (before `return`):
```jsx
  const sel = selectedLeg != null ? legs[selectedLeg] : null;
  const stats = useMemo(
    () => (payoff ? computeStats(payoff.curve, payoff.breakevens, spot) : null),
    [payoff, spot]);
  const chartWindow = useMemo(() => {
    if (!payoff) return null;
    if (spot == null) return null; // full curve
    return [spot * (1 - rangePct / 100), spot * (1 + rangePct / 100)];
  }, [payoff, spot, rangePct]);
  const legStrikes = useMemo(() => [...new Set(legs.map((l) => l.strike))], [legs]);
```

Layout — replace the first `<section className="card">…</section>` (underlying row + legs table) and the Payoff section with the screenshot order. The top card becomes:
```jsx
      <section className="card">
        <div className="row gap center">
          <input className="input narrow" value={underlying}
            onChange={(e) => { setUnderlying(e.target.value.toUpperCase()); setSpot(null); }} placeholder="Underlying" />
          <button className="btn" onClick={loadChain}>Load chain</button>
          {spot != null && <span className="price-chip">${spot.toFixed(2)}</span>}
          <span className="muted small">{chain.expirations.length} expiries · {chain.strikes.length} strikes</span>
        </div>

        {chain.expirations.length ? (
          <>
            <div className="muted small" style={{ marginTop: 10 }}>EXPIRATION{sel?.expiry ? `: ${sel.expiry}` : ""}</div>
            <ExpiryTabs expirations={chain.expirations} value={sel?.expiry || null} onChange={pickExpiry} />
            <div className="muted small">STRIKE:</div>
            <StrikeRuler strikes={chain.strikes} value={sel?.strike ?? null}
              right={sel?.right || "C"} action={sel?.action || "BUY"} spot={spot} onChange={pickStrike} />
          </>
        ) : (
          <div className="muted" style={{ marginTop: 10 }}>Load a chain to pick expiry and strike.</div>
        )}

        <StatsRow netDebit={netDebit} stats={stats} />

        {payoff ? (
          <PayoffChart curve={payoff.curve} breakevens={payoff.breakevens}
            strikes={legStrikes} spot={spot} window={chartWindow} />
        ) : (
          <div className="muted">Fetch quotes, then Preview to see the payoff.</div>
        )}

        <div className="row gap center sliders-row">
          <label className="muted small">RANGE: ±{rangePct}%</label>
          <input type="range" min="2" max="50" step="1" value={rangePct}
            onChange={(e) => setRangePct(Number(e.target.value))} />
          <label className="muted small">IMPLIED VOLATILITY: {(Number(iv) * 100).toFixed(1)}%</label>
          <input type="range" min="0.05" max="1.5" step="0.01" value={iv}
            onChange={(e) => setIv(e.target.value)}
            onPointerUp={() => { if (netDebit != null) preview(); }} />
          <button className="btn" onClick={preview} disabled={busy || !legs.length}>Preview</button>
        </div>

        <h4>Legs</h4>
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Side</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Mid</th><th></th></tr></thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i} className={i === selectedLeg ? "leg-selected" : ""}
                  onClick={() => setSelectedLeg(i)}>
                  <td><select value={l.action} onChange={(e) => setLeg(i, { action: e.target.value })}><option>BUY</option><option>SELL</option></select></td>
                  <td><select value={l.right} onChange={(e) => setLeg(i, { right: e.target.value })}><option value="C">Call</option><option value="P">Put</option></select></td>
                  <td><select value={l.strike} onChange={(e) => setLeg(i, { strike: Number(e.target.value) })}>{chain.strikes.map((s) => <option key={s} value={s}>{s}</option>)}</select></td>
                  <td><select value={l.expiry} onChange={(e) => setLeg(i, { expiry: e.target.value })}>{chain.expirations.map((x) => <option key={x} value={x}>{x}</option>)}</select></td>
                  <td><input className="input tiny" type="number" min="1" value={l.qty} onChange={(e) => setLeg(i, { qty: Number(e.target.value) })} /></td>
                  <td className="num">{l.mid == null ? "—" : l.mid.toFixed(2)}</td>
                  <td><button className="btn ghost tiny" onClick={(e) => { e.stopPropagation(); removeLeg(i); }}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="row gap center">
          <button className="btn ghost" onClick={addLeg} disabled={!chain.expirations.length}>+ Add leg</button>
          <button className="btn" onClick={fetchQuotes} disabled={busy || !legs.length}>Fetch quotes</button>
          <span className="muted">Net: <b>{debitLabel}</b></span>
        </div>
      </section>
```
Delete the old standalone Payoff `<section>` entirely (its IV input and Preview button moved into the sliders row; the `eval_date` note moves under the chart if desired — keep the line `<div className="muted small">At {payoff.eval_date}</div>` directly under `<PayoffChart …/>` inside the ternary). Pass the new callback to ChainGrid:
```jsx
      <ChainGrid key={underlying} underlying={underlying} expirations={chain.expirations}
        onAddLeg={addLegFromChain} onSpot={setSpot} />
```
The Save section and Tracked trades section are unchanged.

- [ ] **Step 3: Append the CSS**

Append to `frontend/src/styles.css`:

```css
/* --- Builder visual chain: integration --- */
.price-chip { background: #16351f; color: #22c55e; font-weight: 700; border-radius: 8px;
  padding: 3px 10px; font-size: 14px; }
.sliders-row { margin: 8px 0 14px; flex-wrap: wrap; }
.sliders-row input[type="range"] { flex: 1 1 140px; min-width: 120px; accent-color: #2563eb; }
tr.leg-selected td { background: #1b2436; }
tr.leg-selected td:first-child { box-shadow: inset 3px 0 0 #2563eb; }
```

- [ ] **Step 4: Build + node test + commit**

```bash
cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend
node src/components/builder/payoffStats.test.mjs   # still ALL PAYOFF-STATS TESTS PASSED
VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"   # ✓ built in …
git add frontend/src/components/BuilderTab.jsx frontend/src/components/ChainGrid.jsx frontend/src/styles.css
git commit -m "feat(builder-ui): selected-leg model, screenshot layout, RANGE/IV sliders"
```

---

### Task 6: Deploy + manual verification

**Files:** none (verification only; commit only if fixes needed).

- [ ] **Step 1: Deploy**

```bash
cd /Users/karanpatel/PycharmProjects/Tradding-system/frontend && VITE_API_BASE="" npm run build 2>&1 | grep "built in"
launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web
for i in $(seq 1 45); do curl -s -m3 http://127.0.0.1:8765/health 2>/dev/null | grep -q '"status":"ok"' && echo OK && break; sleep 1; done
```

- [ ] **Step 2: API smoke (data the new UI consumes)**

```bash
curl -s -m30 "http://127.0.0.1:8765/api/builder/chain?underlying=QQQ" | python3 -c "import sys,json;d=json.load(sys.stdin);print('expiries',len(d['expirations']),'strikes',len(d['strikes']))"
```
Expected: non-zero expiries + strikes.

- [ ] **Step 3: Manual checklist (user in browser, Builder tab)**

- Load chain → expiry tab bar shows month groups with day numbers; click a day.
- Strike ruler scrolls/centers; click + drag snaps; badge shows e.g. 725C green (BUY) / red when leg set to SELL.
- Add a second leg (calendar), click between rows — pickers follow the selected row.
- Fetch quotes → Preview → stats row (net debit / max loss / max profit / breakeven +%) and gradient chart with blue breakeven line, dotted strikes.
- RANGE slider zooms the window; IV slider re-previews on release.
- Save flow unchanged (target/stop/contracts → tracked trade appears).

- [ ] **Step 4: Commit any fixes**

```bash
git add -A && git commit -m "fix(builder-ui): verification tweaks" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** ExpiryTabs (T2), StrikeRuler w/ badge+spot+drag (T3), StatsRow + infinite detection + price-0 extrapolation + movePct (T1+T4), gradient chart w/ breakeven/strike/spot markers + window clipping (T4), RANGE+IV sliders (T5), selected-leg model incl. create-on-first-interaction and delete-reselect (T5), header price chip via ChainGrid onSpot (T5), error/degenerate handling (T1 tests + null guards in components), save/ChainGrid flow untouched (T5 keeps sections). Non-goals honored (no date scrubber, no chance-of-profit, no backend changes).
- **Spec deviation (deliberate, flag to user):** the header chip shows the spot **price only** — no day-change %, because no existing endpoint returns prev-close for arbitrary underlyings and backend changes are out of scope. Also spot arrives only after the chain grid loads quotes (that's the existing fetch the spec pointed at).
- **Type consistency:** `computeStats(curve, breakevens, spot)` shape matches StatsRow's `stats` prop and BuilderTab's `useMemo`; `PayoffChart` new props are all optional with old call-site defaults; `onSpot` optional (ChainGrid used elsewhere? — it is only used by BuilderTab).
- **Placeholder scan:** clean — every step carries complete code; no TBDs.
- **Known risk for implementers:** `emptyLeg` is defined at module scope in BuilderTab (line 8) — `pickExpiry`/`pickStrike` reuse it; `setSelectedLeg(legs.length)` inside the same render tick as `setLegs` relies on `legs` being the pre-update array (correct: the new leg's index equals the old length).
