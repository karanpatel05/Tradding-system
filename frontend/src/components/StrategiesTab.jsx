import { useEffect, useState } from "react";
import { api } from "../api.js";
import StrategyDetail from "./StrategyDetail.jsx";
import "./strategies.css";

const usd = (n) =>
  n == null || !Number.isFinite(Number(n))
    ? "—"
    : Number(n).toLocaleString("en-US", { style: "currency", currency: "USD" });
const signedUsd = (n) =>
  n == null || !Number.isFinite(Number(n)) ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + usd(Math.abs(n));
const pct = (n, digits = 0) =>
  n == null || !Number.isFinite(Number(n)) ? "—" : `${(n * 100).toFixed(digits)}%`;
const tone = (n) => (n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "");
const sessionDateLabel = (iso) => {
  const d = iso ? new Date(iso + "T12:00:00Z") : new Date();
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: iso ? "UTC" : "America/New_York" });
};

// One consistent status vocabulary for the table and the selected-strategy card.
export function strategyStatus(s) {
  if (!s.ready) return { label: "Coming soon", cls: "soon" };
  if (s.running) return { label: "Running", cls: "live" };
  if (s.external_runner) return { label: "External", cls: "external", title: s.activation_blocked || "Runs outside this app" };
  if (s.activation_blocked) return { label: "Blocked", cls: "blocked", title: s.activation_blocked };
  return { label: "Paused", cls: "idle" };
}
const canToggle = (s) => s.ready && !s.external_runner && (s.running || !s.activation_blocked);

// Start/Pause with an inline confirm; the backend still enforces auth and readiness.
function ToggleButton({ s, confirming, busy, onAsk, onConfirm, onCancel, primary }) {
  if (!canToggle(s)) return null;
  const verb = s.running ? "Pause" : "Start";
  if (confirming)
    return (
      <span className="strat-confirm">
        <button className={"btn tiny " + (s.running ? "danger" : "primary")} disabled={busy} onClick={onConfirm}>
          {busy ? "Working…" : `Confirm ${verb.toLowerCase()}`}
        </button>
        <button className="btn ghost tiny" disabled={busy} onClick={onCancel}>Cancel</button>
      </span>
    );
  return (
    <button className={primary ? "btn primary" : "btn ghost tiny"} onClick={onAsk} aria-label={`${verb} ${s.name}`}>
      {primary ? `${verb} strategy` : verb}
    </button>
  );
}

function ResearchSnapshot({ strategy, record, error }) {
  let body;
  if (error) body = <p className="muted small">Research results unavailable: {error}</p>;
  else if (!record) body = <p className="muted small" role="status">Loading…</p>;
  else if (record.source === "backtest")
    body = (
      <>
        <p className="muted">Backtest of {strategy.name} on {record.symbol || "its universe"}.</p>
        <div className="strat-metrics">
          <div><span>Trades</span><b>{record.trades ?? "—"}</b></div>
          <div><span>Win rate</span><b>{pct(record.win_rate)}</b></div>
          <div><span>Max drawdown</span><b className="neg">{pct(record.max_drawdown_pct, 1)}</b></div>
        </div>
        <p className="muted small">{record.timeframe} window, {record.first_date} to {record.last_date}.</p>
      </>
    );
  else if (record.source === "paper")
    body = (
      <>
        <p className="muted">Simulated sessions recorded by this app for {strategy.name}.</p>
        <div className="strat-metrics">
          <div><span>Sessions</span><b>{record.sessions}</b></div>
          <div><span>Win rate</span><b>{pct(record.win_rate)}</b></div>
          <div><span>Max drawdown</span><b className={record.max_drawdown < 0 ? "neg" : ""}>{signedUsd(record.max_drawdown)}</b></div>
        </div>
        <p className="muted small">Net {signedUsd(record.net_pnl)} · {record.first_date} to {record.last_date} · from the app's trade ledger, not a backtest.</p>
      </>
    );
  else if (record.source === "external")
    body = <p className="muted">{record.note}</p>;
  else body = <p className="muted">No results yet. Simulated sessions appear here after the first closed trade.</p>;
  return (
    <section className="card strat-research" aria-label="Research snapshot">
      <h2>Research snapshot</h2>
      {body}
    </section>
  );
}

export default function StrategiesTab({ focusKey, onFocusHandled }) {
  const [strategies, setStrategies] = useState([]);
  const [sessionDate, setSessionDate] = useState(null);
  const [detailKey, setDetailKey] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [confirming, setConfirming] = useState(null);
  const [busy, setBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState(null);
  const [record, setRecord] = useState(null);
  const [recordError, setRecordError] = useState(null);

  const load = () => {
    setLoading(true);
    return api
      .strategies()
      .then((d) => {
        const list = d.strategies || [];
        setStrategies(list);
        setSessionDate(d.session_date || null);
        setError(null);
        setSelectedKey((k) => (k && list.some((s) => s.key === k && s.ready) ? k : list.find((s) => s.ready)?.key || null));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    load();
  }, []);
  useEffect(() => {
    if (focusKey) {
      setDetailKey(focusKey);
      onFocusHandled?.();
    }
  }, [focusKey, onFocusHandled]);
  useEffect(() => {
    if (!selectedKey) return;
    let alive = true;
    setRecord(null);
    setRecordError(null);
    api
      .strategyRecord(selectedKey)
      .then((r) => alive && setRecord(r))
      .catch((e) => alive && setRecordError(e.status === 404 ? "this server does not provide it yet" : e.message));
    return () => {
      alive = false;
    };
  }, [selectedKey]);

  if (detailKey)
    return (
      <StrategyDetail
        key={detailKey}
        keyName={detailKey}
        onBack={() => {
          setDetailKey(null);
          load();
        }}
      />
    );

  const ready = strategies.filter((s) => s.ready);
  const placeholders = strategies.filter((s) => !s.ready);
  const running = ready.filter((s) => s.running);
  const allocated = running.reduce((n, s) => n + (s.capital_allocation || 0), 0);
  const pnls = ready.map((s) => s.session_pnl).filter((v) => v != null && Number.isFinite(v));
  const sessionPnl = pnls.length ? pnls.reduce((a, b) => a + b, 0) : null;
  const executions = [...new Set(ready.map((s) => s.execution).filter(Boolean))];
  const selected = ready.find((s) => s.key === selectedKey) || null;

  const toggle = async (s) => {
    setBusy(true);
    setActionMsg(null);
    try {
      await (s.running ? api.stop(s.key) : api.activate(s.key));
      setActionMsg({ ok: true, text: `${s.name} ${s.running ? "paused" : "started"}.` });
      await load();
    } catch (e) {
      setActionMsg({
        ok: false,
        text: e.status === 401 ? "Starting or pausing needs the app access token (Connections)." : `Could not ${s.running ? "pause" : "start"} ${s.name}: ${e.message}`,
      });
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };
  const toggleProps = (s, primary) => ({
    s,
    primary,
    busy,
    confirming: confirming === s.key + (primary ? ":card" : ":row"),
    onAsk: () => setConfirming(s.key + (primary ? ":card" : ":row")),
    onConfirm: () => toggle(s),
    onCancel: () => setConfirming(null),
  });

  return (
    <div className="strategies-tab">
      <header className="page-head">
        <div>
          <h1>Strategies</h1>
          <p className="page-description">Monitor the rules. Control the allocation. Know what is running.</p>
        </div>
        <div className="strat-session">
          <div>{sessionDateLabel(sessionDate)}</div>
          <div className="muted small">New York session</div>
          <button className="btn ghost tiny" onClick={load} disabled={loading} aria-label="Refresh strategies">
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>
      {error && (
        <div className="banner error" role="alert">
          Could not load strategies: {error}
        </div>
      )}
      {actionMsg && (
        <div className={"banner " + (actionMsg.ok ? "ok" : "error")} role="status">
          {actionMsg.text}
        </div>
      )}
      <div className="stat-strip operator-metrics">
        <div className="stat">
          <div className="stat-label">Running</div>
          <div className="stat-value">{loading || error ? "—" : running.length}</div>
          <div className="stat-sub muted">of {ready.length} strateg{ready.length === 1 ? "y" : "ies"}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Allocated capital</div>
          <div className="stat-value">{loading || error ? "—" : usd(allocated)}</div>
          <div className="stat-sub muted">Running strategies</div>
        </div>
        <div className="stat">
          <div className="stat-label">Session P&amp;L</div>
          <div className={"stat-value " + tone(sessionPnl)}>{loading || error ? "—" : signedUsd(sessionPnl)}</div>
          <div className="stat-sub muted">Realized today, app-tracked strategies</div>
        </div>
        <div className="stat">
          <div className="stat-label">Execution</div>
          <div className="stat-value">Paper</div>
          <div className="stat-sub muted">{executions.length ? executions.join(" · ") : "See Connections for routing"}</div>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <h2>Strategy workspace</h2>
        </div>
        {loading && !strategies.length ? (
          <div className="empty" role="status">Loading strategies…</div>
        ) : !ready.length ? (
          <div className="empty">{error ? "Strategy data is unavailable. Try Refresh." : "No strategies are ready yet."}</div>
        ) : (
          <div className="table-scroll">
            <table className="table strat-table">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Universe</th>
                  <th>Allocation</th>
                  <th>Session P&amp;L</th>
                  <th>Status</th>
                  <th aria-label="Action" />
                </tr>
              </thead>
              <tbody>
                {ready.map((s) => {
                  const st = strategyStatus(s);
                  return (
                    <tr key={s.key} className={s.key === selectedKey ? "selected" : ""} onClick={() => setSelectedKey(s.key)}>
                      <td>
                        <button className="link-btn" onClick={(e) => { e.stopPropagation(); setSelectedKey(s.key); }}>{s.name}</button>
                        {s.category && <div className="strat-sub">{s.category}</div>}
                      </td>
                      <td>{s.universe?.length ? s.universe.join(", ") : s.selected_symbol || "—"}</td>
                      <td className="num">{usd(s.capital_allocation || 0)}</td>
                      <td className={"num " + tone(s.session_pnl)} title={s.session_pnl == null ? (s.external_runner ? "Tracked by the external runner, not this app" : "Not reported by this server yet") : undefined}>
                        {signedUsd(s.session_pnl)}
                      </td>
                      <td><span className={`pill ${st.cls}`} title={st.title}>{st.label}</span></td>
                      <td className="strat-action" onClick={(e) => e.stopPropagation()}>
                        {canToggle(s) ? <ToggleButton {...toggleProps(s, false)} /> : <span className="muted small" title={st.title}>{s.external_runner ? "Runs externally" : "Unavailable"}</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {placeholders.length > 0 && (
          <p className="muted small strat-note">
            {placeholders.length} more strateg{placeholders.length === 1 ? "y is" : "ies are"} in development ({placeholders.map((s) => s.name).join(", ")}).
          </p>
        )}
      </section>

      {selected && (
        <div className="strat-bottom">
          <section className="card strat-selected" aria-label={`${selected.name} details`}>
            <div className="strat-selected-head">
              <h2>{selected.name}</h2>
              <span className="pill selected-tag">Selected strategy</span>
            </div>
            {selected.description && <p className="muted strat-desc">{selected.description}</p>}
            <div className="strat-facts">
              <div><span>Universe</span><b>{selected.universe?.length ? selected.universe.join(", ") : selected.selected_symbol || "—"}</b></div>
              <div><span>Capital allocation</span><b>{usd(selected.capital_allocation || 0)}</b></div>
              <div><span>Execution</span><b>{selected.execution || "—"}</b></div>
              <div><span>Status</span><b><span className={`pill ${strategyStatus(selected).cls}`}>{strategyStatus(selected).label}</span></b></div>
            </div>
            {!canToggle(selected) && strategyStatus(selected).title && <p className="muted small">{strategyStatus(selected).title}</p>}
            <div className="strat-actions">
              <ToggleButton {...toggleProps(selected, true)} />
              <button className="btn" onClick={() => setDetailKey(selected.key)}>Open full details</button>
            </div>
          </section>
          <ResearchSnapshot strategy={selected} record={record} error={recordError} />
        </div>
      )}
    </div>
  );
}
