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
