// frontend/src/components/builder/GreeksPanel.jsx
// Net position greeks as four glass tiles under the stat cards. Values are the
// POSITION totals from /api/builder/greeks (options x100 x qty; stock legs x qty
// shares for delta): Δ in share-equivalents, Θ in $/day, ν in $/vol-point.

const TILES = [
  { key: "delta", sym: "Δ", label: "DELTA", dp: 2 },
  { key: "gamma", sym: "Γ", label: "GAMMA", dp: 4 },
  { key: "theta", sym: "Θ", label: "THETA $/DAY", dp: 2 },
  { key: "vega", sym: "ν", label: "VEGA $/VOL-PT", dp: 2 },
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
            <span className="stat-icon" style={{ color: "#4F7CFF", background: "#4F7CFF22", fontSize: 14 }}>
              {sym}
            </span>
            <div className="stat-body">
              <div className="stat-label">{label}</div>
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
