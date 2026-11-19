import { useEffect, useState } from "react";
import { api } from "../api.js";
import StrategyDetail from "./StrategyDetail.jsx";

export default function StrategiesTab({ focusKey, onFocusHandled }) {
  const [strategies, setStrategies] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const load = () => {
    setLoading(true);
    return api
      .strategies()
      .then((d) => {
        setStrategies(d.strategies || []);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    load();
  }, []);
  useEffect(() => {
    if (focusKey) {
      setSelected(focusKey);
      onFocusHandled?.();
    }
  }, [focusKey, onFocusHandled]);
  if (selected)
    return (
      <StrategyDetail
        key={selected}
        keyName={selected}
        onBack={() => {
          setSelected(null);
          load();
        }}
      />
    );
  const active = strategies.filter((s) => s.running);
  return (
    <div className="strategies-tab">
      <header className="page-head">
        <div>
          <h1>Strategies</h1>
          <p className="page-description">
            Monitor the rules. Control the allocation. Know what is running.
          </p>
        </div>
        <button className="btn" onClick={load} disabled={loading}>
          Refresh
        </button>
      </header>
      {error && (
        <div className="banner error" role="alert">
          Could not load strategies: {error}
        </div>
      )}
      <div className="stat-strip operator-metrics">
        <div className="stat">
          <div className="stat-label">Running</div>
          <div className="stat-value">
            {loading || error ? "—" : active.length}
          </div>
          <div className="stat-sub muted">Across available strategies</div>
        </div>
        <div className="stat">
          <div className="stat-label">Allocated capital</div>
          <div className="stat-value">
            {loading || error
              ? "—"
              : "$" +
                active
                  .reduce((n, s) => n + (s.capital_allocation || 0), 0)
                  .toLocaleString()}
          </div>
          <div className="stat-sub muted">Running strategy budgets</div>
        </div>
        <div className="stat">
          <div className="stat-label">Available strategies</div>
          <div className="stat-value">
            {loading || error ? "—" : strategies.filter((s) => s.ready).length}
          </div>
          <div className="stat-sub muted">Rules implemented</div>
        </div>
        <div className="stat">
          <div className="stat-label">Execution</div>
          <div className="stat-value">Paper</div>
          <div className="stat-sub muted">See Connections for routing</div>
        </div>
      </div>
      <section className="card">
        <div className="card-head">
          <h2>Strategy workspace</h2>
          <span className="small muted">
            Select a strategy to review and manage
          </span>
        </div>
        {loading ? (
          <div className="empty" role="status">
            Loading strategies…
          </div>
        ) : !strategies.length ? (
          <div className="empty">
            {error
              ? "Strategy data is unavailable. Try Refresh."
              : "No strategies are available."}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Selected symbol</th>
                  <th>Capital allocation</th>
                  <th>Status</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((s) => (
                  <tr key={s.key}>
                    <td>
                      <button
                        className="link-btn"
                        onClick={() => setSelected(s.key)}
                      >
                        {s.name}
                      </button>
                    </td>
                    <td>{s.selected_symbol || "—"}</td>
                    <td className="num">
                      ${(s.capital_allocation || 0).toLocaleString()}
                    </td>
                    <td>
                      <span
                        className={`pill ${!s.ready ? "soon" : s.running ? "live" : "idle"}`}
                        title={s.activation_blocked || undefined}
                      >
                        {!s.ready
                          ? "Coming soon"
                          : s.activation_blocked && !s.running
                            ? "Off"
                            : s.status}
                      </span>
                    </td>
                    <td>
                      <button
                        className="btn ghost tiny"
                        onClick={() => setSelected(s.key)}
                        aria-label={`Open ${s.name}`}
                      >
                        Open ↗
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
