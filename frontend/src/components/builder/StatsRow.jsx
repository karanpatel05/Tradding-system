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
