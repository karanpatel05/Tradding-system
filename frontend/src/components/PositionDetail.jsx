import { useEffect, useState } from "react";
import { api } from "../api.js";

const money = (n, d = 2) =>
  n === null || n === undefined ? "—" : `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
const num = (n, d = 2) => (n === null || n === undefined ? "—" : Number(n).toFixed(d));
const pct = (n) => (n === null || n === undefined ? "—" : `${(n * 100).toFixed(2)}%`);
const time = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

// Live detail for the strategy's single open position: fill, TP/SL, live price,
// unrealized P&L, R-multiple, and a stop→target progress bar. Polls every 5s.
export default function PositionDetail({ keyName, onBack }) {
  const [p, setP] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    const tick = () => api.position(keyName)
      .then((d) => { if (alive) { setP(d); setErr(null); } })
      .catch((e) => { if (alive) setErr(e.message); });
    tick();
    const id = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(id); };
  }, [keyName]);

  if (err) return (
    <div className="ema-page">
      <button className="btn ghost" onClick={onBack}>← Back</button>
      <div className="banner error">Couldn’t load position: {err}</div>
    </div>
  );
  if (!p) return (
    <div className="ema-page">
      <button className="btn ghost" onClick={onBack}>← Back</button>
      <div className="muted">Loading…</div>
    </div>
  );
  if (!p.has_position) return (
    <div className="ema-page">
      <button className="btn ghost" onClick={onBack}>← Back</button>
      <section className="card"><h2>No open position</h2>
        <div className="muted">The strategy is currently flat.</div></section>
    </div>
  );

  const up = p.unrealized_pnl;
  const tone = up == null ? "" : up >= 0 ? "pos" : "neg";
  // Stop→Target progress bar: 0% at stop, 100% at target, marker at live price.
  let markerPct = null;
  if (p.stop != null && p.target != null && p.live_price != null && p.target !== p.stop) {
    markerPct = Math.max(0, Math.min(100, ((p.live_price - p.stop) / (p.target - p.stop)) * 100));
  }

  return (
    <div className="ema-page">
      <button className="btn ghost" onClick={onBack}>← Back</button>

      <section className="card">
        <div className="card-head">
          <h1>{p.symbol} — Open Position</h1>
          <span className={`pill ${p.side === "long" ? "live" : "idle"}`}>{p.side} ×{p.quantity}</span>
        </div>

        <div className="stat-strip" style={{ marginTop: 12 }}>
          <Stat label="Unrealized P&L" value={money(up)} tone={tone} />
          <Stat label="Return" value={pct(p.unrealized_pct)} tone={tone} />
          <Stat label="R multiple" value={p.r_multiple != null ? `${num(p.r_multiple, 2)}R` : "—"} />
          <Stat label="Live price" value={p.live_price != null ? num(p.live_price) : "—"}
            sub={p.live_price == null ? "connect for live" : "updating"} />
        </div>
      </section>

      {/* Stop → Target progress */}
      {markerPct != null && (
        <section className="card">
          <h2>Stop → Target</h2>
          <div className="pos-bar">
            <div className="pos-bar-fill" style={{ width: `${markerPct}%` }} />
            <div className="pos-bar-marker" style={{ left: `${markerPct}%` }} />
          </div>
          <div className="pos-bar-ends">
            <span className="neg">SL {num(p.stop)}</span>
            <span className="muted">live {num(p.live_price)}</span>
            <span className="pos">TP {num(p.target)}</span>
          </div>
        </section>
      )}

      <section className="card">
        <h2>Details</h2>
        <div className="metric-grid">
          <Metric label="Symbol" value={p.symbol} />
          <Metric label="Side" value={p.side} />
          <Metric label="Quantity" value={p.quantity} />
          <Metric label="Fill price (entry)" value={num(p.fill_price)} />
          <Metric label="Take profit (TP)" value={num(p.target)} tone="pos" />
          <Metric label="Stop loss (SL)" value={num(p.stop)} tone="neg" />
          <Metric label="Live price" value={p.live_price != null ? num(p.live_price) : "—"} />
          <Metric label="Risk / share" value={money(p.risk_per_share)} />
          <Metric label="Reward / share" value={money(p.reward_per_share)} />
          <Metric label="Notional" value={money(p.notional)} />
          <Metric label="Entry time" value={time(p.entry_time)} />
          <Metric label="Progress to TP" value={p.progress_to_target != null ? pct(p.progress_to_target) : "—"} />
        </div>
        <div className="muted small" style={{ marginTop: 10 }}>
          Live price + P&L refresh every 5s while connected. The strategy holds one position at a time.
        </div>
      </section>
    </div>
  );
}

const Stat = ({ label, value, sub, tone }) => (
  <div className="stat">
    <div className="stat-label">{label}</div>
    <div className={`stat-value ${tone || ""}`}>{value}</div>
    {sub && <div className="stat-sub muted small">{sub}</div>}
  </div>
);
const Metric = ({ label, value, tone }) => (
  <div className="metric"><div className="metric-label">{label}</div>
    <div className={`metric-value ${tone || ""}`}>{value}</div></div>
);
