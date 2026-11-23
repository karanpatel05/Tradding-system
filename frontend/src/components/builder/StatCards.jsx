// frontend/src/components/builder/StatCards.jsx
import { useEffect, useRef, useState } from "react";

// Stat cards: label, tabular figure and the change since the last quote refresh.
const usd = (n) => `$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;


const Trend = ({ now, prev, invert = false }) => {
  if (now == null || prev == null || typeof now !== "number" || typeof prev !== "number"
      || Math.abs(now - prev) < 0.005) return null;
  const up = now > prev;
  const good = invert ? !up : up;
  return (
    <span className={`stat-trend ${good ? "pos" : "neg"}`} title="Change since the last quote refresh">
      {up ? "▲" : "▼"} {usd(Math.abs(now - prev))}
    </span>
  );
};

function Card({ label, children, trend }) {
  return (
    <div className="stat-card">
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
      <Card label={netDebit != null && netDebit < 0 ? "Net credit" : "Net debit"}
        trend={<Trend now={debit$} prev={snap.debit} invert />}>
        {debit$ == null ? "—" : usd(debit$)}
      </Card>
      <Card label="Max loss" trend={<Trend now={stats?.maxLoss} prev={snap.loss} />}>
        {fmt(stats?.maxLoss, "neg")}
      </Card>
      <Card label="Max profit" trend={<Trend now={stats?.maxProfit} prev={snap.profit} />}>
        {fmt(stats?.maxProfit, "pos")}
      </Card>
      <Card label="Prob. of profit"
        trend={<Trend now={chance == null ? null : chance * 100} prev={snap.chance == null ? null : snap.chance * 100} />}>
        {chance == null ? "—" : <span className={chance >= 0.5 ? "pos" : ""}>{(chance * 100).toFixed(0)}%</span>}
      </Card>
      <Card label="Breakeven">
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
