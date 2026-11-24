// frontend/src/components/builder/GreeksPanel.jsx
// Net position greeks as four tiles under the stat cards. Values are the
// POSITION totals from /api/builder/greeks (options x100 x qty; stock legs x qty
// shares for delta): Δ in share-equivalents, Θ in $/day, ν in $/vol-point.

const TILES = [
  { key: "delta", sym: "Δ", label: "Delta", dp: 2 },
  { key: "gamma", sym: "Γ", label: "Gamma", dp: 4 },
  { key: "theta", sym: "Θ", label: "Theta $/day", dp: 2 },
  { key: "vega", sym: "ν", label: "Vega $/vol pt", dp: 2 },
];

export default function GreeksPanel({ greeks }) {
  const net = greeks?.net;
  if (!net) return null;
  return (
    <div className="stat-cards greeks-panel">
      {TILES.map(({ key, sym, label, dp }) => {
        const v = net[key];
        const cls = v == null || v === 0 ? "" : v > 0 ? "pos" : "neg";
        return (
          <div className="stat-card" key={key}>
            <div className="stat-body">
              <div className="stat-label"><span className="stat-sym">{sym}</span> {label}</div>
              <div className={`stat-num ${cls}`}>
                {v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(dp)}`}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
