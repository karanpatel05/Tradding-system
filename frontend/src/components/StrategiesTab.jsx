import { useEffect, useState } from "react";
import { api } from "../api.js";
import StrategyDetail from "./StrategyDetail.jsx";

export default function StrategiesTab({ focusKey, onFocusHandled }) {
  const [strategies, setStrategies] = useState([]);
  const [selected, setSelected] = useState(null);

  const load = () => api.strategies().then((d) => setStrategies(d.strategies)).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  // Opened from the dashboard with a specific strategy in focus → jump to its page.
  useEffect(() => {
    if (focusKey) { setSelected(focusKey); onFocusHandled?.(); }
  }, [focusKey]);

  if (selected) {
    const onBack = () => { setSelected(null); load(); };
    return <StrategyDetail keyName={selected} onBack={onBack} />;
  }

  return (
    <div className="strategies-tab">
      <h1>Strategies</h1>
      <div className="strategy-grid">
        {strategies.map((s) => (
          <div
            key={s.key}
            className={`card strategy-card ${s.ready ? "" : "disabled"}`}
            onClick={() => setSelected(s.key)}
          >
            <div className="row between center">
              <h3>{s.name}</h3>
              {s.ready ? (
                <span className={`pill ${s.running ? "live" : "idle"}`}>{s.status}</span>
              ) : (
                <span className="pill soon">Coming Soon</span>
              )}
            </div>
            <div className="muted small">
              {s.ready ? `Capital: $${(s.capital_allocation || 0).toLocaleString()}` : "Awaiting strategy rules"}
            </div>
            {s.ready && s.selected_symbol && (
              <div className="selected-sym">Selected: {s.selected_symbol}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
