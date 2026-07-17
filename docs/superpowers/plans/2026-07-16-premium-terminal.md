# Premium Terminal Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Institutional-grade terminal redesign — deep-navy glass design system across the app, rebuilt Builder (stat cards, expiry rail, strike-slider polish, zoom/pan payoff centerpiece, modern legs table with templates/hedge/stock legs), upgraded sidebar, then Greeks panel + Δ overlay + IV smile.

**Architecture:** Design-token overhaul in place (CSS custom properties in `styles.css`; all components share it). Component upgrades in existing files; one new backend capability per phase (stock legs; greeks/IV endpoints on tested Black-Scholes math). No new frontend dependencies.

**Tech Stack:** React 18 + Recharts + plain CSS (existing); FastAPI + `app/options/blackscholes.py` extensions.

**Spec:** `docs/superpowers/specs/2026-07-16-premium-terminal-design.md`

## Global Constraints

- Palette EXACT: `--bg:#060B18`, `--card:#0F172A`, accent `#4F7CFF`, pos `#34D399`, neg `#F87171`, radius 16px, hairline `rgba(148,163,184,0.12)`; glass = `rgba(15,23,42,0.72)` + `backdrop-filter: blur(12px)`.
- Numbers always `tabular-nums`; motion 150–250ms ease (300ms charts); CSS only — NO new npm deps.
- Builder stays paper-only: stock legs must never appear in `app/broker/` paths (grep-gated Task 3 & 11: `grep -rn '"S"\|right.*S' backend/app/broker` clean of stock-leg handling).
- Leg mutations keep flowing through `setLeg`/`invalidateQuote` (stale net_debit must never survive) — templates/hedge/duplicate included.
- Templates replace legs only via arm-confirm (two clicks ≤5s apart).
- Backend tests: `cd backend && rm -f _t_X.db && DATABASE_URL=sqlite+aiosqlite:///./_t_X.db ../.venv/bin/python -m pytest <files> -q; rm -f _t_X.db` (tests/ refuses the live DB). Frontend gate per task: `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → `✓ built in …` AND `node src/components/builder/payoffStats.test.mjs` → ALL PASSED.
- Venv python `../.venv/bin/python` from `backend/`.

## File Structure

```
frontend/src/styles.css                        (T1 retune tokens/global; touched by most tasks)
frontend/src/App.jsx                           (T2 sidebar: glow/collapsible/footer)
backend/app/options/combo.py                   (T3 stock-leg pricing)
backend/app/options/chain.py                   (T3 get_leg_mids stock branch)
backend/app/routers/builder.py                 (T3 Leg "S", quote/payoff guards; T9/T10 endpoints)
frontend/src/components/builder/StatCards.jsx  (T4 new; replaces StatsRow usage)
frontend/src/components/BuilderTab.jsx         (T4 header chip; T6 zoom/pan state; T7 toolbar; T8 layout)
frontend/src/components/builder/ExpiryTabs.jsx (T5 rail rewrite, same props)
frontend/src/components/builder/StrikeRuler.jsx(T5 hover/tooltip)
frontend/src/components/PayoffChart.jsx        (T6 height/animation/Δ-toggle prop; T9 delta overlay)
frontend/src/components/builder/templates.js   (T7 new: pure leg generators + hedge)
frontend/src/components/builder/GreeksPanel.jsx(T9 new)
frontend/src/components/builder/IvSmile.jsx    (T10 new)
backend/app/options/blackscholes.py            (T9 bs_greeks; T10 implied_vol)
backend/tests/test_stock_legs.py (T3) / test_bs_greeks.py (T9) / test_iv_smile.py (T10)
frontend/src/components/builder/payoffStats.{js,test.mjs} (T3 stock-leg netCalls case)
```

---

### Task 1: Design tokens + global restyle

**Files:** Modify `frontend/src/styles.css` (the `:root` block, `body`, `.card`, `.btn`, `.pill`, `.input`, `.table`, `select`).

**Interfaces:** Produces the token set every later task consumes: `--bg --card --card-glass --accent --pos --neg --radius --border --shadow-1 --glow-accent` (old names `--surface --green --red --accent-soft` etc. remain as ALIASES so untouched components keep working).

- [ ] **Step 1: Replace the `:root` block** in `frontend/src/styles.css` with:

```css
:root {
  /* premium terminal tokens */
  --bg: #060B18;
  --card: #0F172A;
  --card-glass: rgba(15, 23, 42, 0.72);
  --accent: #4F7CFF;
  --accent-bright: #7C9BFF;
  --accent-soft: rgba(79, 124, 255, 0.14);
  --pos: #34D399;
  --neg: #F87171;
  --amber: #FBBF24;
  --blue: #60A5FA;
  --violet: #C4B5FD;
  --text: #E8EDF4;
  --muted: #8A94A6;
  --faint: #5C6677;
  --border: rgba(148, 163, 184, 0.12);
  --border-soft: rgba(148, 163, 184, 0.07);
  --radius: 16px;
  --radius-sm: 11px;
  --shadow-1: 0 4px 24px rgba(0, 0, 0, 0.35);
  --shadow-2: 0 12px 40px -12px rgba(0, 0, 0, 0.6);
  --glow-accent: 0 0 0 1px rgba(79, 124, 255, 0.35), 0 4px 20px rgba(79, 124, 255, 0.25);
  --num: "SF Mono", "JetBrains Mono", ui-monospace, "Roboto Mono", monospace;
  /* aliases so pre-redesign components keep resolving */
  --surface: var(--card);
  --surface-2: #141D31;
  --surface-3: #1B2540;
  --green: var(--pos);
  --red: var(--neg);
}
```

- [ ] **Step 2: Body + card + interactive base styles** — replace the existing `body` rule's `background` line with:

```css
  background:
    radial-gradient(1100px 620px at 85% -8%, rgba(79, 124, 255, 0.10) 0%, transparent 55%),
    radial-gradient(900px 500px at -10% 110%, rgba(52, 211, 153, 0.05) 0%, transparent 50%),
    var(--bg);
```

and REPLACE the current `.card` rule (find it; it sets background/border/radius) with:

```css
.card {
  position: relative;
  background: linear-gradient(180deg, rgba(148, 163, 184, 0.035), transparent 42%), var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  box-shadow: var(--shadow-1);
  transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
  animation: cardIn 320ms ease both;
}
.card::before {
  content: ""; position: absolute; inset: 0 12% auto 12%; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(79, 124, 255, 0.35), transparent);
  border-radius: 1px; pointer-events: none;
}
.card:hover { transform: translateY(-2px); box-shadow: var(--shadow-2); border-color: rgba(148, 163, 184, 0.2); }
@keyframes cardIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .card, .card:hover { animation: none; transform: none; } }
```

- [ ] **Step 3: Buttons, pills, inputs, selects, tables** — update the existing rules (keep selectors, replace bodies):
  - `.btn`: radius `var(--radius-sm)`, border `1px solid var(--border)`, background `var(--surface-2)`, `transition: all 160ms ease`, hover `border-color: rgba(79,124,255,.45); box-shadow: var(--glow-accent); transform: translateY(-1px)`. `.btn.primary`: background `linear-gradient(180deg, #5B85FF, var(--accent))`, no border, `box-shadow: 0 4px 16px rgba(79,124,255,.35)`.
  - `.pill`: add `backdrop-filter: blur(8px); transition: all 160ms ease;`.
  - `.input, select`: background `rgba(6,11,24,.6)`, border `1px solid var(--border)`, radius `var(--radius-sm)`, focus `outline: none; box-shadow: var(--glow-accent); border-color: var(--accent)`.
  - `.table th`: 11px uppercase letterspaced muted; `.table td { font-variant-numeric: tabular-nums; }`; row hover `background: rgba(79,124,255,.05)`.
  - Add utility: `.num, .stat-value, .big-pnl { font-variant-numeric: tabular-nums; }`.

- [ ] **Step 4: Build + visual sanity.** `cd frontend && VITE_API_BASE="" npm run build 2>&1 | grep -E "built in|error"` → ✓. Deploy is NOT required per task; the Task-8 screenshot gate covers visuals.

- [ ] **Step 5: Commit** — `git add frontend/src/styles.css && git commit -m "feat(theme): premium terminal tokens — deep navy, glass, electric blue, motion"`

---

### Task 2: Sidebar — glow, collapsible groups, account footer

**Files:** Modify `frontend/src/App.jsx` (nav render only), `frontend/src/styles.css` (append).

**Interfaces:** Consumes existing `Icon` component and `TABS`. Produces grouped nav: `NAV_GROUPS = [{label:"TRADING", tabs:["Dashboard","Strategies","Builder"]},{label:"ANALYSIS", tabs:["Journal","API"]}]`.

- [ ] **Step 1:** In `App.jsx`, replace the flat `TABS.map` nav with grouped collapsible sections:

```jsx
const NAV_GROUPS = [
  { label: "TRADING", tabs: ["Dashboard", "Strategies", "Builder"] },
  { label: "ANALYSIS", tabs: ["Journal", "API"] },
];
```

Inside the component add:

```jsx
  const [collapsed, setCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem("navCollapsed") || "{}"); } catch { return {}; }
  });
  const toggleGroup = (label) => setCollapsed((c) => {
    const next = { ...c, [label]: !c[label] };
    localStorage.setItem("navCollapsed", JSON.stringify(next));
    return next;
  });
  const [acct, setAcct] = useState(null);
  useEffect(() => {
    const load = () => api.account().then((d) => setAcct(d.account || d)).catch(() => {});
    load(); const id = setInterval(load, 60000); return () => clearInterval(id);
  }, []);
```

Nav render (replaces the existing `<nav className="nav">…</nav>` body):

```jsx
        <nav className="nav">
          {NAV_GROUPS.map((g) => (
            <div className="nav-group" key={g.label}>
              <button className="nav-group-head" onClick={() => toggleGroup(g.label)}>
                <span>{g.label}</span>
                <span className={`chev ${collapsed[g.label] ? "closed" : ""}`}>▾</span>
              </button>
              {!collapsed[g.label] && g.tabs.map((t) => (
                <button key={t} className={`nav-item ${tab === t ? "active" : ""}`}
                  onClick={() => { setTab(t); setFocusKey(null); }}>
                  <Icon name={t} /> <span>{t}</span>
                </button>
              ))}
            </div>
          ))}
        </nav>
```

(Match the existing nav-item click handler semantics — read the current onClick and keep any extra behavior it has.) Footer, after `</nav>` inside the `<aside>`:

```jsx
        <div className="side-foot">
          <div className="avatar">{(auth?.account || "T").replace(/[^A-Za-z0-9]/g, "").slice(-1) || "T"}</div>
          <div className="side-foot-meta">
            <div className="side-foot-acct">{auth?.account || "—"}</div>
            <div className="side-foot-nl num">{acct?.net_liquidation != null ? `$${Number(acct.net_liquidation).toLocaleString()}` : "—"}</div>
          </div>
          <span className={`dot ${auth?.connected ? "on" : ""}`} title={auth?.connected ? "Broker verified" : "Broker not verified"} />
        </div>
```

- [ ] **Step 2: CSS (append):**

```css
/* --- premium sidebar --- */
.nav-group-head { display: flex; justify-content: space-between; width: 100%; background: none;
  border: none; color: var(--faint); font-size: 10px; letter-spacing: .12em; padding: 10px 12px 4px;
  cursor: pointer; }
.nav-group-head .chev { transition: transform 180ms ease; }
.nav-group-head .chev.closed { transform: rotate(-90deg); }
.nav-item { position: relative; }
.nav-item.active { background: var(--accent-soft); color: var(--accent-bright);
  box-shadow: inset 3px 0 0 var(--accent), 0 0 18px rgba(79,124,255,.12); }
.nav-item.active svg { filter: drop-shadow(0 0 6px rgba(79,124,255,.7)); }
.side-foot { display: flex; align-items: center; gap: 10px; margin-top: auto;
  padding: 12px; border-top: 1px solid var(--border-soft); }
.avatar { width: 32px; height: 32px; border-radius: 10px; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--accent), #7C5CFF); font-weight: 700; font-size: 14px; }
.side-foot-acct { font-size: 11px; color: var(--muted); }
.side-foot-nl { font-size: 13px; font-weight: 650; }
.side-foot .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--neg); margin-left: auto; }
.side-foot .dot.on { background: var(--pos); box-shadow: 0 0 8px rgba(52,211,153,.7); }
```

(If `.nav-item`/`.sidebar` base rules don't exist under those names, read the current sidebar CSS class names and adapt selectors — keep semantics.)

- [ ] **Step 3:** Build gate ✓ → commit `feat(ui): sidebar — grouped collapsible nav, active glow, account footer`.

---

### Task 3: Stock legs (backend + payoffStats)

**Files:** Modify `backend/app/routers/builder.py` (Leg model + payoff/quote guards + `_strike_span`), `backend/app/options/combo.py` (`payoff_at`, `combo_value` — read them first), `backend/app/options/chain.py` (`get_leg_mids`); `frontend/src/components/builder/payoffStats.js` (netCalls: stock counts as delta but NOT a call — right-edge slope from stock is linear: treat BUY stock like +calls for unboundedness: `netCalls += qty/100 sign` NO — simpler and correct: stock legs make the right edge unbounded in their direction; add `netStock = Σ sign·qty` and unbounded-up if `netCalls > 0 or netStock > 0`, unbounded-down if `netCalls < 0 or netStock < 0`). Test: `backend/tests/test_stock_legs.py` + payoffStats case.

**Interfaces:** Leg `right` gains `"S"` (strike ignored → send 0; expiry may be `""`). `payoff_at` prices S-legs as `S` (linear). `/quote` prices S-legs at facade spot, `theo=False` always for them. `/payoff` `eval_date` default derives from OPTION legs only (all-stock → today).

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_stock_legs.py
"""Stock legs (right='S'): linear payoff, spot-priced quotes (never theo), payoff
endpoint tolerates empty expiry, covered-call structure values correctly."""
from datetime import date, timedelta

import pytest

from app.options.combo import combo_value, payoff_at
from app.routers import builder

EXP = (date.today() + timedelta(days=30)).isoformat()


def test_payoff_at_stock_linear():
    legs = [{"action": "BUY", "right": "S", "strike": 0.0, "expiry": "", "qty": 100}]
    assert payoff_at(legs, 50.0, date.today(), 0.2, 0.04) == pytest.approx(5000.0)
    assert payoff_at(legs, 60.0, date.today(), 0.2, 0.04) == pytest.approx(6000.0)


def test_covered_call_value():
    legs = [{"action": "BUY", "right": "S", "strike": 0.0, "expiry": "", "qty": 100},
            {"action": "SELL", "right": "C", "strike": 105.0, "expiry": EXP, "qty": 1}]
    # deep past expiry, at S=120: stock 12000 - intrinsic short call 15*1 = 11985 (per-1x*qty units)
    v = payoff_at(legs, 120.0, date.fromisoformat(EXP) + timedelta(days=1), 0.2, 0.0)
    assert v == pytest.approx(120.0 * 100 - 15.0, abs=0.01)


def test_combo_value_stock_uses_mid():
    legs = [{"action": "BUY", "right": "S", "strike": 0.0, "expiry": "", "qty": 100}]
    assert combo_value(legs, [101.5]) == pytest.approx(101.5 * 100)


@pytest.mark.asyncio
async def test_quote_stock_leg_spot_never_theo(monkeypatch):
    async def fake_mids(underlying, legs):
        return [None] * len(legs)      # options side missing
    monkeypatch.setattr(builder.chain_mod, "get_leg_mids", fake_mids)
    body = builder.QuoteBody(underlying="QQQ", spot=100.0, iv=0.25, legs=[
        builder.Leg(action="BUY", right="S", strike=0.0, expiry="", qty=100)])
    out = await builder.quote(body)
    assert out["legs"][0]["mid"] == 100.0 and out["legs"][0]["theo"] is False
    assert out["theoretical"] is False


@pytest.mark.asyncio
async def test_payoff_all_stock_defaults_today(monkeypatch):
    body = builder.PayoffBody(net_debit=100.0, iv=0.25, spot=100.0, legs=[
        builder.Leg(action="BUY", right="S", strike=0.0, expiry="", qty=100)])
    out = await builder.payoff(body)
    assert out["eval_date"] == date.today().isoformat()
    assert len(out["curve"]) >= 2
```

Run (expect FAIL: Literal rejects "S"): `cd backend && rm -f _t_sl.db && DATABASE_URL=sqlite+aiosqlite:///./_t_sl.db ../.venv/bin/python -m pytest tests/test_stock_legs.py -q; rm -f _t_sl.db`

- [ ] **Step 2: Implement.** Read `combo.py` top (LEG_SIGN/_qty/_expiry) then:
  - `payoff_at`: first line of the loop → `if leg["right"] == "S": total += LEG_SIGN[leg["action"]] * _qty(leg) * S; continue` (before `_expiry`).
  - `combo_value` already multiplies mid×sign×qty — verify `_qty` uses `qty` (it does for options); no change if generic. If it special-cases contracts×100 anywhere, stock legs use raw qty (shares) — adjust accordingly and note it.
  - `chance_of_profit`/`curve_stats` operate on the curve — unchanged.
  - `routers/builder.py`: `Leg.right: Literal["C", "P", "S"]`; `expiry: str = ""`. `/quote`: before the theo loop, price S-legs: `if leg["right"] == "S": mids[i] = body.spot if (body.spot or 0) > 0 else None; theo[i] = False` (a missing spot leaves it missing — no fill). `chain.get_leg_mids`: S-legs return None placeholder (router fills them) — add branch to skip the option fetch: `async def _one(leg): return None if leg["right"] == "S" else await src.aoption_mid(...)`. `/payoff` eval default: `opt = [l for l in legs if l["right"] != "S"]; ev = ... min over opt if opt else date.today()`. `_strike_span`: `strikes = [l["strike"] for l in legs if l["right"] != "S"] or [body-spot-fallback 100.0]`.
  - `payoffStats.js`: in `computeStats`, `netCallQty` section gains `let netStock = 0; ... if (l.right === "S") { netStock += sign * qty; return; }` and unbounded rules become `netCalls > 0 || netStock > 0 → maxProfit infinite`, `netCalls < 0 || netStock < 0 → maxLoss infinite`. Add test case in `payoffStats.test.mjs`: covered call (BUY S qty 100 + SELL C) → netCalls −1, netStock +100 → maxProfit "infinite"? NO — covered call: upside flat (stock +100 shares ≈ +1 delta, short call −1 → net 0 above strike). Per-contract convention: options count ±1 per contract (≡100 shares); so stock legs contribute `sign * qty / 100`. Use that: `netStock += sign * qty / 100`; unbounded via `(netCalls + netStock) > 0` on the upside and `(netCalls + netStock) < 0` downside for the RIGHT edge... downside (price→0) stays bounded ALWAYS (stock to zero is finite) — so: right edge unbounded-profit iff `netCalls + netStock > 0`; unbounded-loss iff `netCalls + netStock < 0`. Covered call: −1 + 1 = 0 → bounded ✓. Test asserts covered call bounded both sides, long stock alone → maxProfit infinite.

- [ ] **Step 3:** All tests green (backend file + payoffStats node test + full backend suite) → build gate → commit `feat(builder): stock legs — linear pricing, spot quotes, covered-call-ready stats`.

---

### Task 4: Stat cards + header market chip

**Files:** Create `frontend/src/components/builder/StatCards.jsx`; modify `BuilderTab.jsx` (swap StatsRow → StatCards; add market-status chip via `api.marketStatus()`), `styles.css` (append). Delete usage of `StatsRow.jsx` (leave the file; removed in Task 8 cleanup).

**Interfaces:** `<StatCards netDebit stats chance />` — same inputs StatsRow had. Trend memory internal (useRef of previous {debit$, maxLoss, maxProfit, chance}).

- [ ] **Step 1: Component**

```jsx
// frontend/src/components/builder/StatCards.jsx
import { useEffect, useRef, useState } from "react";

// Premium stat cards: icon chip + large tabular number + small trend-vs-last-refresh.
const usd = (n) => `$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const ICONS = {
  debit: ["#4F7CFF", "M12 1a11 11 0 1 0 0 22 11 11 0 0 0 0-22zm1 6v2h3v2h-5a1 1 0 0 0 0 2h4a3 3 0 0 1 0 6h-1v2h-2v-2H9v-2h5a1 1 0 0 0 0-2h-4a3 3 0 0 1 0-6h1V7h2z"],
  loss: ["#F87171", "M3 5l7 7-4 4h12V4l-4 4-7-7z"],
  profit: ["#34D399", "M3 19l7-7-4-4h12v12l-4-4-7 7z"],
  pop: ["#4F7CFF", "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zm3 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm8 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"],
  be: ["#8A94A6", "M4 11h12l-4-4 1.4-1.4L20 12l-6.6 6.4L12 17l4-4H4v-2z"],
};

const Trend = ({ now, prev, invert = false }) => {
  if (now == null || prev == null || typeof now !== "number" || typeof prev !== "number"
      || Math.abs(now - prev) < 0.005) return null;
  const up = now > prev;
  const good = invert ? !up : up;
  return <span className={`stat-trend ${good ? "pos" : "neg"}`}>{up ? "▲" : "▼"} {usd(Math.abs(now - prev))}</span>;
};

function Card({ icon, label, children, trend }) {
  const [c, d] = ICONS[icon];
  return (
    <div className="stat-card">
      <span className="stat-icon" style={{ color: c, background: `${c}22` }}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d={d} /></svg>
      </span>
      <div className="stat-body">
        <div className="stat-label">{label}</div>
        <div className="stat-num">{children}</div>
        {trend}
      </div>
    </div>
  );
}

export default function StatCards({ netDebit, stats, chance }) {
  const debit$ = netDebit == null ? null : netDebit * 100;
  const prev = useRef({});
  const [snap, setSnap] = useState({});
  useEffect(() => {
    if (netDebit == null) { prev.current = {}; setSnap({}); return; }   // legs changed/reset
    setSnap(prev.current);
    prev.current = { debit: debit$, loss: stats?.maxLoss, profit: stats?.maxProfit, chance };
  }, [debit$, stats?.maxLoss, stats?.maxProfit, chance]);   // eslint-disable-line

  const fmt = (v, cls) => v == null ? "—" : v === "infinite"
    ? <span className={cls}>Infinite</span> : <span className={cls}>{usd(v)}</span>;
  const bes = stats?.breakevens || [];
  return (
    <div className="stat-cards">
      <Card icon="debit" label={netDebit != null && netDebit < 0 ? "NET CREDIT" : "NET DEBIT"}
        trend={<Trend now={debit$} prev={snap.debit} invert />}>
        {debit$ == null ? "—" : usd(debit$)}
      </Card>
      <Card icon="loss" label="MAX LOSS" trend={<Trend now={stats?.maxLoss} prev={snap.loss} />}>
        {fmt(stats?.maxLoss, "neg")}
      </Card>
      <Card icon="profit" label="MAX PROFIT" trend={<Trend now={stats?.maxProfit} prev={snap.profit} />}>
        {fmt(stats?.maxProfit, "pos")}
      </Card>
      <Card icon="pop" label="PROB. OF PROFIT"
        trend={<Trend now={chance == null ? null : chance * 100} prev={snap.chance == null ? null : snap.chance * 100} />}>
        {chance == null ? "—" : <span className={chance >= 0.5 ? "pos" : ""}>{(chance * 100).toFixed(0)}%</span>}
      </Card>
      <Card icon="be" label="BREAKEVEN">
        {bes.length === 0 ? "—" : bes.map((b, i) => (
          <span key={i} className="be-item">
            {i > 0 && " / "}
            {bes.length === 1 && b.movePct != null && (b.movePct >= 0 ? "Above " : "Below ")}
            {usd(b.price)}
            {b.movePct != null && <span className="muted small"> ({b.movePct >= 0 ? "+" : ""}{(b.movePct * 100).toFixed(1)}%)</span>}
          </span>
        ))}
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: CSS (append):**

```css
/* --- stat cards --- */
.stat-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 14px 0; }
.stat-card { display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px;
  background: var(--card-glass); backdrop-filter: blur(12px); border: 1px solid var(--border);
  border-radius: var(--radius); transition: transform 180ms ease, box-shadow 180ms ease;
  animation: cardIn 320ms ease both; }
.stat-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-2); }
.stat-icon { width: 28px; height: 28px; border-radius: 9px; display: grid; place-items: center; flex: none; }
.stat-card .stat-label { font-size: 10px; letter-spacing: .08em; color: var(--muted); }
.stat-num { font-size: 21px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
.stat-num .pos { color: var(--pos); } .stat-num .neg { color: var(--neg); }
.stat-trend { display: block; font-size: 10px; margin-top: 2px; font-variant-numeric: tabular-nums; }
.stat-trend.pos { color: var(--pos); } .stat-trend.neg { color: var(--neg); }
.be-item { font-size: 15px; }
```

- [ ] **Step 3: Wire in BuilderTab:** replace `<StatsRow netDebit={netDebit} stats={stats} chance={payoff?.chance_of_profit} />` with `<StatCards …same props… />` (import swap). Header market chip: add state `const [mkt, setMkt] = useState(null);` + effect polling `api.marketStatus()` every 30s; render next to the price: `<span className={`mkt-chip ${mkt?.is_open ? "open" : "closed"}`}><span className="mkt-dot" />{mkt?.is_open ? "Market Open" : (mkt?.session === "closed" ? "Closed" : mkt?.session?.replace("_", "-"))}</span>`. CSS: `.mkt-chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;font-size:11px;background:var(--card-glass);border:1px solid var(--border);} .mkt-chip .mkt-dot{width:7px;height:7px;border-radius:50%;background:var(--neg);} .mkt-chip.open .mkt-dot{background:var(--pos);box-shadow:0 0 8px rgba(52,211,153,.8);animation:pulse 2s infinite;} @keyframes pulse{50%{opacity:.5;}}` (verify `api.marketStatus()` exists in api.js — it does — and its payload keys via `services/market.get_market_status`).

- [ ] **Step 4:** Build + payoffStats node gates ✓ → commit `feat(builder-ui): stat cards with icons and trend deltas + market-status chip`.

---

### Task 5: Expiry rail + strike-slider hover polish

**Files:** Rewrite `frontend/src/components/builder/ExpiryTabs.jsx` (same props); modify `StrikeRuler.jsx` (hover state + tooltip); `styles.css` (append; the old `.xtabs` CSS block may be removed).

- [ ] **Step 1: ExpiryTabs rewrite** (props unchanged: `{expirations, value, onChange}`):

```jsx
// frontend/src/components/builder/ExpiryTabs.jsx
import { useEffect, useMemo, useRef } from "react";

// Sleek horizontal date rail: day pills with floating month labels, snap scrolling,
// accent glow on the active pill.
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function ExpiryTabs({ expirations = [], value, onChange }) {
  const ref = useRef(null);
  const items = useMemo(() => {
    const thisYear = new Date().getFullYear();
    let lastKey = "";
    return expirations.map((iso) => {
      const [y, m, d] = iso.split("-").map(Number);
      const key = `${y}-${m}`;
      const label = key !== lastKey
        ? `${MON[m - 1]}${y !== thisYear ? ` '${String(y).slice(2)}` : ""}` : null;
      lastKey = key;
      return { iso, day: d, month: label };
    });
  }, [expirations]);

  useEffect(() => {
    ref.current?.querySelector(".xr-pill.active")
      ?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [value]);

  if (!expirations.length) return null;
  return (
    <div className="xrail" ref={ref}>
      {items.map(({ iso, day, month }) => (
        <div className="xr-cell" key={iso}>
          <div className="xr-month">{month || " "}</div>
          <button type="button" className={`xr-pill ${iso === value ? "active" : ""}`}
            onClick={() => onChange(iso)}>{day}</button>
        </div>
      ))}
    </div>
  );
}
```

CSS (append; delete the old `.xtabs`/`.xt-*` block):

```css
/* --- expiry rail --- */
.xrail { display: flex; gap: 6px; overflow-x: auto; padding: 4px 2px 10px;
  scroll-snap-type: x proximity; scrollbar-width: thin; }
.xr-cell { flex: 0 0 auto; text-align: center; scroll-snap-align: center; }
.xr-month { font-size: 9px; letter-spacing: .08em; color: var(--faint); height: 13px; }
.xr-pill { min-width: 38px; padding: 7px 9px; border-radius: 12px; border: 1px solid var(--border);
  background: var(--card-glass); backdrop-filter: blur(8px); color: var(--muted); font-size: 13px;
  cursor: pointer; transition: all 160ms ease; font-variant-numeric: tabular-nums; }
.xr-pill:hover { color: var(--text); border-color: rgba(79,124,255,.4); transform: translateY(-1px); }
.xr-pill.active { background: linear-gradient(180deg, #5B85FF, var(--accent)); color: #fff;
  border-color: transparent; font-weight: 700; box-shadow: var(--glow-accent); }
```

- [ ] **Step 2: StrikeRuler hover.** Add state `const [hover, setHover] = useState(null); // {i, x}` — in the existing `onPointerMove`, when NOT dragging compute the hovered index (same math as `pick`) and `setHover({ i, x: i * TICK_W + TICK_W / 2 })`; `onPointerLeave={() => setHover(null)}`. Render inside the track: `{hover != null && strikes[hover.i] != null && (<><div className="sruler-hovline" style={{ left: hover.x }} /><div className="sruler-tip num" style={{ left: hover.x }}>{strikes[hover.i]}</div></>)}`. Badge transition CSS + tooltip + edge fades:

```css
/* --- strike ruler polish --- */
.sruler { position: relative; }
.sruler::before, .sruler::after { content: ""; position: sticky; top: 0; width: 28px; height: 0;
  display: block; z-index: 2; pointer-events: none; }
.sruler-badge { transition: left 140ms ease; }
.sruler-hovline { position: absolute; top: 0; width: 1px; height: 26px; background: rgba(79,124,255,.6); }
.sruler-tip { position: absolute; top: -22px; transform: translateX(-50%); padding: 2px 8px;
  border-radius: 8px; background: var(--card-glass); backdrop-filter: blur(8px);
  border: 1px solid var(--border); font-size: 11px; color: var(--text); pointer-events: none; }
```

(If the sticky-fade trick fights the layout, replace `::before/::after` with two absolutely-positioned gradient divs inside `.sruler` — implementer's choice; note it.)

- [ ] **Step 3:** Build gate ✓ → commit `feat(builder-ui): expiry date rail + strike ruler hover tooltip`.

---

### Task 6: Payoff centerpiece — zoom, pan, animation

**Files:** Modify `frontend/src/components/PayoffChart.jsx` (height 380, animation on, accept `onWheelZoom`), `BuilderTab.jsx` (wheel/pan state + handlers + Δ-toggle placeholder), `styles.css`.

**Interfaces:** BuilderTab gains `panPct` state; `chartWindow` becomes `[spot*(1−r/100)+off, spot*(1+r/100)+off]` where `off = spot * panPct / 100`, clamped so the window ⊆ curve span; `panPct` resets on `invalidateQuote`. PayoffChart new props: `height = 380`, `animate = true` (300ms), plus a wrapper div in BuilderTab handling wheel/drag.

- [ ] **Step 1:** PayoffChart: `height` prop (default 380) on ResponsiveContainer; `<Area ... isAnimationActive={true} animationDuration={300} animationEasing="ease" />`.
- [ ] **Step 2:** BuilderTab wrapper around the chart:

```jsx
      <div className="chart-zone"
        onWheel={(e) => {
          if (!payoff || spot == null) return;
          e.preventDefault();
          setRangePct((r) => Math.min(50, Math.max(2, Math.round(r * (e.deltaY > 0 ? 1.15 : 0.87)))));
        }}
        onPointerDown={(e) => { dragRef.current = { x: e.clientX, pan: panPct }; }}
        onPointerMove={(e) => {
          if (!dragRef.current || spot == null) return;
          const dxPct = ((e.clientX - dragRef.current.x) / e.currentTarget.clientWidth) * rangePct * 2;
          setPanPct(clampPan(dragRef.current.pan - dxPct));
        }}
        onPointerUp={() => { dragRef.current = null; }}
        onDoubleClick={() => { setRangePct(8); setPanPct(0); }}
        title="Scroll to zoom · drag to pan · double-click to reset">
```

with `const dragRef = useRef(null); const [panPct, setPanPct] = useState(0);` and `const clampPan = (p) => Math.min(50 - rangePct, Math.max(-(50 - rangePct), p));` (curve spans ±50% of spot when spot is known); `chartWindow` adds `spot * panPct / 100` to both bounds; `invalidateQuote` also `setPanPct(0)`. NOTE: React onWheel is passive — attach a non-passive listener via ref (`useEffect` + `el.addEventListener("wheel", fn, { passive: false })`) so `preventDefault` works; the implementer must do it that way, not the JSX prop.
- [ ] **Step 3:** Δ toggle placeholder next to the sliders row: `<button className="btn ghost" disabled title="Greeks overlay — coming in Phase 2">Δ overlay</button>`. CSS: `.chart-zone { cursor: grab; } .chart-zone:active { cursor: grabbing; }`.
- [ ] **Step 4:** Build gate ✓ → commit `feat(builder-ui): payoff centerpiece — wheel zoom, drag pan, animated transitions`.

---

### Task 7: Legs table + toolbar (templates, hedge, duplicate)

**Files:** Create `frontend/src/components/builder/templates.js`; modify `BuilderTab.jsx` (table redesign + toolbar), `styles.css`.

**Interfaces:** `templates.js` exports pure functions consumed by BuilderTab:
`buildTemplate(name, {expiry, strikes, spot}) -> legs[] | null` for names `"vertical" | "straddle" | "butterfly" | "iron_condor" | "covered_call"`; `buildHedge(legs, {spot, strikesByExpiry}) -> leg | null`.

- [ ] **Step 1: templates.js**

```javascript
// frontend/src/components/builder/templates.js
// Pure strategy-template generators. ATM = nearest strike to spot; "step" = index
// step in the expiry's strike list; all outputs are plain leg dicts.
const atmIndex = (strikes, spot) => {
  let best = 0;
  strikes.forEach((s, i) => { if (Math.abs(s - spot) < Math.abs(strikes[best] - spot)) best = i; });
  return best;
};
const at = (strikes, i) => strikes[Math.min(strikes.length - 1, Math.max(0, i))];
const leg = (action, right, strike, expiry, qty = 1) => ({ action, right, strike, expiry, qty });

export function buildTemplate(name, { expiry, strikes, spot }) {
  if (!expiry || !strikes?.length || spot == null) return null;
  const i = atmIndex(strikes, spot);
  switch (name) {
    case "vertical":
      return [leg("BUY", "C", at(strikes, i), expiry), leg("SELL", "C", at(strikes, i + 2), expiry)];
    case "straddle":
      return [leg("BUY", "C", at(strikes, i), expiry), leg("BUY", "P", at(strikes, i), expiry)];
    case "butterfly":
      return [leg("BUY", "C", at(strikes, i - 2), expiry), leg("SELL", "C", at(strikes, i), expiry, 2),
              leg("BUY", "C", at(strikes, i + 2), expiry)];
    case "iron_condor":
      return [leg("SELL", "P", at(strikes, i - 2), expiry), leg("BUY", "P", at(strikes, i - 4), expiry),
              leg("SELL", "C", at(strikes, i + 2), expiry), leg("BUY", "C", at(strikes, i + 4), expiry)];
    case "covered_call":
      return [leg("BUY", "S", 0, "", 100), leg("SELL", "C", at(strikes, i + 2), expiry)];
    default:
      return null;
  }
}

// Directional exposure sign (per-contract; stock counts qty/100). Positive -> long bias.
export function netBias(legs) {
  let n = 0;
  for (const l of legs) {
    const sign = l.action === "BUY" ? 1 : -1;
    if (l.right === "C") n += sign;
    else if (l.right === "P") n -= sign;
    else if (l.right === "S") n += sign * (Number(l.qty) || 0) / 100;
  }
  return n;
}

export function buildHedge(legs, { spot, strikesFor }) {
  if (!legs.length || spot == null) return null;
  const bias = netBias(legs);
  if (bias === 0) return null;
  const optionLegs = legs.filter((l) => l.right !== "S" && l.expiry);
  const expiry = optionLegs.map((l) => l.expiry).sort()[0] || null;
  if (!expiry) return null;
  const strikes = strikesFor(expiry);
  if (!strikes?.length) return null;
  const target = bias > 0 ? spot * 0.95 : spot * 1.05;
  const k = strikes.reduce((a, b) => (Math.abs(b - target) < Math.abs(a - target) ? b : a));
  return leg("BUY", bias > 0 ? "P" : "C", k, expiry);
}
```

- [ ] **Step 2: BuilderTab toolbar + table.** Above the table:

```jsx
        <div className="legs-toolbar">
          <h4 style={{ margin: 0 }}>LEGS</h4>
          <div className="row gap center">
            <button className="btn ghost" onClick={addLeg} disabled={!chain.expirations.length}>+ Leg</button>
            <div className="tmpl-wrap">
              <button className="btn ghost" onClick={() => setTmplOpen((o) => !o)} disabled={!chain.expirations.length || spot == null}>Templates ▾</button>
              {tmplOpen && (
                <div className="tmpl-menu">
                  {[["vertical", "Vertical Spread"], ["straddle", "Straddle"], ["butterfly", "Butterfly"],
                    ["iron_condor", "Iron Condor"], ["covered_call", "Covered Call"]].map(([k, label]) => (
                    <button key={k} className={`tmpl-item ${armedTmpl === k ? "armed" : ""}`}
                      onClick={() => applyTemplate(k)}>
                      {armedTmpl === k ? `Replace legs with ${label}?` : label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button className="btn ghost" onClick={applyHedge} disabled={!legs.length || spot == null}>Add hedge</button>
            <span className="muted">Net: <b className="num">{isTheo ? "~" : ""}{debitLabel}</b></span>
          </div>
        </div>
```

Handlers (arm-confirm ≤5s via a ref timestamp):

```jsx
  const [tmplOpen, setTmplOpen] = useState(false);
  const [armedTmpl, setArmedTmpl] = useState(null);
  const armedAt = useRef(0);
  const applyTemplate = (name) => {
    const now = Date.now();
    if (armedTmpl !== name || now - armedAt.current > 5000) {
      setArmedTmpl(name); armedAt.current = now; return;      // first click arms
    }
    const expiry = sel?.expiry || chain.expirations[0];
    const built = buildTemplate(name, { expiry, strikes: strikesFor(expiry), spot });
    if (built) { setLegs(built); setSelectedLeg(0); invalidateQuote(); }
    setArmedTmpl(null); setTmplOpen(false);
  };
  const applyHedge = () => {
    const h = buildHedge(legs, { spot, strikesFor });
    if (!h) { setMsg("No hedge suggested (flat bias or no option expiry)."); return; }
    setLegs((ls) => [...ls, h]); setSelectedLeg(legs.length); invalidateQuote();
  };
  const duplicateLeg = (i) => {
    setLegs((ls) => [...ls.slice(0, i + 1), { ...ls[i], mid: null }, ...ls.slice(i + 1)]);
    setSelectedLeg(i + 1); invalidateQuote();
  };
```

Table row changes: Side cell → `<button className={`side-badge ${l.action === "BUY" ? "buy" : "sell"}`} onClick={(e) => { e.stopPropagation(); setLeg(i, { action: l.action === "BUY" ? "SELL" : "BUY" }); }}>{l.action}</button>`; Type select gains `<option value="S">Stock</option>` and cell class `right-${l.right}`; strike/expiry selects disabled for S legs (render "—"); actions cell: `⧉` duplicate + `✕` delete (both stopPropagation). CSS:

```css
/* --- legs toolbar/table --- */
.legs-toolbar { display: flex; justify-content: space-between; align-items: center; margin: 14px 0 8px; }
.tmpl-wrap { position: relative; }
.tmpl-menu { position: absolute; top: calc(100% + 6px); left: 0; z-index: 30; min-width: 220px;
  background: var(--card-glass); backdrop-filter: blur(14px); border: 1px solid var(--border);
  border-radius: var(--radius-sm); box-shadow: var(--shadow-2); overflow: hidden; }
.tmpl-item { display: block; width: 100%; text-align: left; padding: 10px 14px; background: none;
  border: none; color: var(--text); font-size: 13px; cursor: pointer; transition: background 140ms ease; }
.tmpl-item:hover { background: var(--accent-soft); }
.tmpl-item.armed { color: var(--amber); }
.side-badge { border: none; border-radius: 8px; padding: 4px 10px; font-size: 11px; font-weight: 700;
  cursor: pointer; transition: transform 140ms ease; }
.side-badge:hover { transform: scale(1.06); }
.side-badge.buy { background: rgba(52,211,153,.16); color: var(--pos); }
.side-badge.sell { background: rgba(248,113,113,.16); color: var(--neg); }
td.right-C select, .right-C { color: var(--pos); }
td.right-P select, .right-P { color: var(--neg); }
td.right-S select, .right-S { color: var(--blue); }
```

Stock-leg row handling in existing logic: `legsKey` fingerprint already includes right/qty ✓; `nearestExpiry` already filters falsy expiry? Read it — it filters `.filter(Boolean)` ✓; the per-expiry strikes effect must skip S legs (guard `!iso`) ✓ by the same falsy check. Verify all three and fix guards if not.

- [ ] **Step 3:** payoffStats node test + build gates ✓ → commit `feat(builder-ui): modern legs table — badges, duplicate, templates, hedge`.

---

### Task 8: Layout tightening, recent trades cards, StatsRow removal, Phase-1 screenshot gate

**Files:** Modify `BuilderTab.jsx` (section order/width), `styles.css`; DELETE `frontend/src/components/builder/StatsRow.jsx` (no longer imported — verify with grep).

- [ ] Tracked trades → card list:

```jsx
      <section className="card">
        <h2>Recent Trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="trade-cards">
            {trades.map((t) => (
              <div className="trade-card" key={t.id}>
                <div className="trade-head">
                  <b>{t.underlying}</b>
                  <span className="muted small">{t.legs.length} legs · {t.entry_time?.slice(0, 10) || "—"}</span>
                </div>
                <div className={`trade-pnl num ${(t.status === "closed" ? t.pnl : t.last_pnl) >= 0 ? "pos" : "neg"}`}>
                  {(() => { const v = t.status === "closed" ? t.pnl : t.last_pnl;
                    return v == null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`; })()}
                </div>
                <span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>{t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span>
                {t.status === "open" && <button className="btn ghost tiny" onClick={() => closeTrade(t.id)}>Close</button>}
              </div>
            ))}
          </div>
        )}
      </section>
```

CSS: `.trade-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;} .trade-card{padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--card-glass);display:flex;flex-direction:column;gap:6px;} .trade-head{display:flex;justify-content:space-between;} .trade-pnl{font-size:18px;font-weight:700;}`
- [ ] Layout: `.builder-tab { max-width: 1280px; margin: 0 auto; }`; tighten section margins to a 16/24 rhythm (audit the page top-to-bottom).
- [ ] `git rm frontend/src/components/builder/StatsRow.jsx` after `grep -rn "StatsRow" frontend/src` shows no imports.
- [ ] Build + node gates ✓ → deploy (`launchctl kickstart -k gui/$(id -u)/com.tradingsystem.web`, wait for health) → **screenshot gate**: run the scratchpad `shot-builder.mjs` + `shot-dash.mjs`, READ both images, self-check against the spec (tokens applied, cards glassy, rail/ruler/toolbar present), fix obvious visual breakage, and SAVE the images for the user report.
- [ ] Commit `feat(builder-ui): premium layout, recent-trades cards; Phase 1 complete`.

---

### Task 9: Greeks — math, endpoint, panel, Δ overlay (Phase 2)

**Files:** Modify `backend/app/options/blackscholes.py` (add `norm_pdf`, `bs_greeks`), `backend/app/routers/builder.py` (POST `/greeks`); create `frontend/src/components/builder/GreeksPanel.jsx`; modify `PayoffChart.jsx` (delta line + secondary axis), `BuilderTab.jsx` (fetch + toggle enable), `api.js`. Test: `backend/tests/test_bs_greeks.py`.

- [ ] **Step 1: Failing test**

```python
# backend/tests/test_bs_greeks.py
"""Analytic BS greeks vs textbook values (S=100,K=100,T=0.25,iv=0.2,r=0.05):
d1=0.175, N(d1)=0.56946, phi(d1)=0.38286... verified below with 2e-3 tolerances."""
import pytest

from app.options.blackscholes import bs_greeks


def test_call_greeks_textbook():
    g = bs_greeks(100.0, 100.0, 0.25, 0.20, 0.05, "C")
    assert g["delta"] == pytest.approx(0.5695, abs=2e-3)
    assert g["gamma"] == pytest.approx(0.0387, abs=2e-3)
    assert g["vega"] == pytest.approx(0.1934, abs=2e-3)     # per 1 vol POINT (=vega/100)
    assert g["theta"] == pytest.approx(-0.0284, abs=2e-3)   # per calendar DAY


def test_put_delta_parity():
    c = bs_greeks(100.0, 100.0, 0.25, 0.20, 0.05, "C")
    p = bs_greeks(100.0, 100.0, 0.25, 0.20, 0.05, "P")
    assert p["delta"] == pytest.approx(c["delta"] - 1.0, abs=1e-9)
    assert p["gamma"] == pytest.approx(c["gamma"], abs=1e-9)
    assert p["vega"] == pytest.approx(c["vega"], abs=1e-9)


def test_stock_and_expired():
    s = bs_greeks(100.0, 0.0, 0.0, 0.2, 0.05, "S")
    assert s == {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    e = bs_greeks(100.0, 90.0, 0.0, 0.2, 0.05, "C")   # expired ITM call -> delta 1
    assert e["delta"] == 1.0 and e["gamma"] == 0.0
```

- [ ] **Step 2: Implement** in `blackscholes.py`:

```python
def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(S: float, K: float, T: float, iv: float, r: float, kind: str) -> dict:
    """Analytic greeks. theta is per CALENDAR DAY; vega per 1 vol POINT (0.01).
    kind 'S' (stock): delta 1, others 0. T<=0: intrinsic deltas, others 0."""
    kind = kind.upper()
    if kind == "S":
        return {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if T <= 0 or iv <= 0:
        if kind == "C":
            d = 1.0 if S > K else 0.0
        else:
            d = -1.0 if S < K else 0.0
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf = norm_pdf(d1)
    gamma = pdf / (S * iv * sqrt_t)
    vega = S * pdf * sqrt_t / 100.0
    if kind == "C":
        delta = norm_cdf(d1)
        theta_y = -S * pdf * iv / (2 * sqrt_t) - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        delta = norm_cdf(d1) - 1.0
        theta_y = -S * pdf * iv / (2 * sqrt_t) + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta_y / 365.0, "vega": vega}
```

Endpoint in `routers/builder.py`:

```python
class GreeksBody(BaseModel):
    legs: list[Leg] = Field(min_length=1)
    spot: float
    iv: float = 0.25
    r: float = 0.04
    curve: bool = False
    lo: float | None = None
    hi: float | None = None


@router.post("/greeks")
async def greeks(body: GreeksBody) -> dict:
    """Per-leg + net position greeks (x100 x qty for options; stock legs x qty shares
    for delta). Optional delta curve across [lo, hi] for the chart overlay."""
    from app.options.blackscholes import bs_greeks
    legs = _legs(body.legs)
    today = date.today()

    def leg_greeks(leg, S):
        T = 0.0 if leg["right"] == "S" or not leg["expiry"] else max(
            (date.fromisoformat(leg["expiry"]) - today).days, 0) / 365.0
        g = bs_greeks(S, leg["strike"] or S, T, body.iv, body.r, leg["right"])
        sign = 1 if leg["action"] == "BUY" else -1
        mult = leg["qty"] if leg["right"] == "S" else 100 * leg["qty"]
        return {k: sign * mult * v for k, v in g.items()}

    per_leg, net = [], {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        g = leg_greeks(leg, body.spot)
        per_leg.append({**{k: round(v, 4) for k, v in g.items()},
                        "right": leg["right"], "strike": leg["strike"], "action": leg["action"]})
        for k in net:
            net[k] += g[k]
    out = {"net": {k: round(v, 4) for k, v in net.items()}, "legs": per_leg}
    if body.curve:
        lo, hi = (body.lo, body.hi) if body.lo is not None and body.hi is not None \
            else (body.spot * 0.5, body.spot * 1.5)
        pts, step = 81, (hi - lo) / 80
        out["delta_curve"] = [
            {"price": round(lo + i * step, 2),
             "delta": round(sum(leg_greeks(l, lo + i * step)["delta"] for l in legs), 2)}
            for i in range(pts)]
    return out
```

- [ ] **Step 3: Frontend.** `api.js`: `builderGreeks: (body) => request("/api/builder/greeks", { method: "POST", body: JSON.stringify(body) }),`. `GreeksPanel.jsx`: four glass tiles (Δ Γ Θ ν) showing `net` values (Δ 2dp; Γ 4dp; Θ as $/day 2dp; ν as $/vol-pt 2dp), colored by sign; renders null without data. BuilderTab: fetch greeks whenever `preview()` succeeds (same body legs/spot/iv + `curve: showDelta`), store `greeks`; render `<GreeksPanel greeks={greeks} />` under StatCards; enable the Δ toggle (`showDelta` state) — when on, pass `deltaCurve={greeks?.delta_curve}` to PayoffChart which renders `<Line yAxisId="delta" dataKey="delta" data={deltaCurve} stroke="var(--accent)" strokeWidth={1.5} dot={false} />` + hidden right `<YAxis yAxisId="delta" orientation="right" hide />` (Recharts: use a composed chart — switch `AreaChart` → `ComposedChart`, imports adjusted; verify Area still renders identically).
- [ ] **Step 4:** Backend tests + full suite + build + node gates ✓ → commit `feat(builder): Greeks — analytic BS, position panel, delta chart overlay`.

---

### Task 10: IV smile (Phase 2)

**Files:** Modify `backend/app/options/blackscholes.py` (`implied_vol`), `backend/app/routers/builder.py` (GET `/iv-smile`); create `frontend/src/components/builder/IvSmile.jsx`; wire into `BuilderTab.jsx` below the chart; `api.js`. Test: `backend/tests/test_iv_smile.py`.

- [ ] **Step 1: Failing test**

```python
# backend/tests/test_iv_smile.py
"""implied_vol inverts bs_price by bisection (round-trip to 1e-3); /iv-smile builds
{strike, iv, side} rows from live mids (calls >= spot, puts < spot), skipping
strikes with no mid or no root."""
from datetime import date, timedelta

import pytest

from app.options.blackscholes import bs_price, implied_vol
from app.routers import builder


def test_round_trip():
    S, K, T, r = 100.0, 105.0, 0.25, 0.04
    for true_iv in (0.12, 0.25, 0.60, 1.50):
        price = bs_price(S, K, T, true_iv, r, "C")
        assert implied_vol(price, S, K, T, r, "C") == pytest.approx(true_iv, abs=1e-3)


def test_no_root_returns_none():
    assert implied_vol(0.0001, 100.0, 200.0, 0.01, 0.04, "C") is None      # below intrinsic floor-ish
    assert implied_vol(1000.0, 100.0, 100.0, 0.25, 0.04, "C") is None      # above any vol


@pytest.mark.asyncio
async def test_smile_endpoint(monkeypatch):
    exp = (date.today() + timedelta(days=30)).isoformat()
    T = 30 / 365.0
    async def fake_chain_quotes(underlying, expiry, count):
        rows = []
        for k, iv in ((95.0, 0.30), (100.0, 0.25), (105.0, 0.28)):
            rows.append({"strike": k,
                         "call_mid": round(bs_price(100.0, k, T, iv, 0.04, "C"), 4),
                         "put_mid": round(bs_price(100.0, k, T, iv, 0.04, "P"), 4)})
        rows.append({"strike": 110.0, "call_mid": None, "put_mid": None})   # skipped
        return {"underlying": underlying, "expiry": expiry, "spot": 100.0,
                "atm": 100.0, "count": count, "rows": rows}
    monkeypatch.setattr(builder.chain_mod, "get_chain_quotes", fake_chain_quotes)
    out = await builder.iv_smile("QQQ", exp, 10)
    got = {(p["strike"], p["side"]): p["iv"] for p in out["points"]}
    assert got[(95.0, "P")] == pytest.approx(0.30, abs=2e-3)    # below spot -> put side
    assert got[(100.0, "C")] == pytest.approx(0.25, abs=2e-3)   # at/above spot -> call side
    assert got[(105.0, "C")] == pytest.approx(0.28, abs=2e-3)
    assert (110.0, "C") not in got and (110.0, "P") not in got
```

- [ ] **Step 2: Implement.** `blackscholes.py`:

```python
def implied_vol(price: float, S: float, K: float, T: float, r: float, kind: str,
                lo: float = 0.01, hi: float = 4.0, tol: float = 1e-4,
                max_iter: int = 60) -> float | None:
    """Invert bs_price by bisection. None when no root brackets [lo, hi]."""
    if price <= 0 or T <= 0:
        return None
    f_lo = bs_price(S, K, T, lo, r, kind) - price
    f_hi = bs_price(S, K, T, hi, r, kind) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = bs_price(S, K, T, mid, r, kind) - price
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2
```

`routers/builder.py`:

```python
@router.get("/iv-smile")
async def iv_smile(underlying: str, expiry: str, count: int = 10) -> dict:
    """Implied-vol smile for one expiry from live mids: put side below spot, call side
    at/above. Strikes without a usable mid or root are skipped."""
    from app.options.blackscholes import implied_vol
    count = max(1, min(int(count), 25))
    data = await chain_mod.get_chain_quotes(underlying, expiry, count)
    spot = data.get("spot")
    points = []
    if spot and spot > 0:
        T = max((date.fromisoformat(expiry) - date.today()).days, 0) / 365.0
        for row in data.get("rows", []):
            k = row["strike"]
            side = "P" if k < spot else "C"
            mid = row["put_mid"] if side == "P" else row["call_mid"]
            if mid is None or T <= 0:
                continue
            iv = implied_vol(mid, spot, k, T, 0.04, side)
            if iv is not None:
                points.append({"strike": k, "iv": round(iv, 4), "side": side})
    return {"underlying": underlying.upper(), "expiry": expiry, "spot": spot,
            "points": points}
```

- [ ] **Step 3: Frontend.** `api.js`: `builderIvSmile: (u, expiry, count = 10) => request(\`/api/builder/iv-smile?underlying=${encodeURIComponent(u)}&expiry=${expiry}&count=${count}\`),`. `IvSmile.jsx`: glass card, Recharts `LineChart` of `points` (x strike, y iv×100 "%"), accent line + dots, spot `ReferenceLine`, empty state "Insufficient quotes for a smile."; refresh button. BuilderTab: render below the chart when `sel?.expiry` is set; fetch on expiry change (debounced 800ms) + on manual refresh only (it fans out ~20 quote subscriptions — do NOT auto-refresh on every quote tick).
- [ ] **Step 4:** Tests + suite + build gates ✓ → commit `feat(builder): implied-volatility smile per expiry`.

---

### Task 11: Full verification + deploy + screenshots

- [ ] Backend: full pytest + the 6 standalone TC tests; frontend: build + payoffStats node test. Grep gates: `grep -rn "StatsRow" frontend/src` empty; stock-leg broker gate `grep -rn '\"S\"' backend/app/broker` shows no stock handling.
- [ ] Deploy; smoke `/api/builder/greeks` (synthetic legs body) and `/api/builder/iv-smile` (live — may be sparse after hours, endpoint must 200 with points possibly []).
- [ ] Screenshots: builder full page (2-leg structure w/ greeks + smile if quotes allow), dashboard; send to the user.
- [ ] Commit any fixes: `test(terminal): end-to-end verification fixes`.

---

## Self-review notes

- **Spec coverage:** tokens/glass/motion (T1); sidebar glow/collapse/footer (T2); stock legs + stats (T3); stat cards + trends + market chip (T4); expiry rail + ruler hover (T5); centerpiece zoom/pan/animation + disabled Δ toggle (T6); legs table badges/duplicate/templates/hedge + arm-confirm (T7); recent-trades cards + tightening + StatsRow removal + screenshot gate (T8); greeks math/endpoint/panel/overlay (T9); IV smile solver/endpoint/chart (T10); verification (T11). Spec's covered-call template consumes T3's stock legs; Δ-toggle enabled in T9 as spec'd.
- **Type consistency:** `buildTemplate/buildHedge/netBias` names used in T7 only; `bs_greeks` dict keys match GreeksBody/leg_greeks/GreeksPanel; `implied_vol` signature matches both tests and endpoint; StatCards props = old StatsRow props.
- **Placeholder scan:** clean. Restyle steps carry full CSS/JSX; directive-style items ("read the current sidebar CSS class names and adapt") are read-the-file adaptations, not omissions.
- **Known risks:** React wheel listeners must be non-passive via ref (called out in T6); Recharts ComposedChart swap in T9 must not regress the gradient Area (visual check); `netStock` convention (qty/100) documented in T3 and tested via covered call; templates assume ≥5 strikes around ATM (at() clamps at edges).
