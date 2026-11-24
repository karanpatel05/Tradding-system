import { useEffect, useState } from "react";
import { api } from "../api.js";
import StrategyDetail from "./StrategyDetail.jsx";
import {
  usd,
  signedUsd,
  pct,
  tone,
  humanize,
  strategyStatus,
  canToggle,
  StatusMark,
  ToggleButton,
  SessionRail,
  scheduleOf,
} from "./strategyUi.jsx";
import "./strategies.css";

const sessionDateLabel = (iso) => {
  const d = iso ? new Date(iso + "T12:00:00Z") : new Date();
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: iso ? "UTC" : "America/New_York" });
};
const universeOf = (s) => (s.universe?.length ? s.universe.join(", ") : s.selected_symbol || "—");

function ResearchSnapshot({ strategy, record, error }) {
  let body;
  if (error) body = <p className="muted small">Research results unavailable: {error}</p>;
  else if (!record) body = <p className="muted small" role="status">Loading…</p>;
  else if (record.source === "backtest")
    body = (
      <>
        <dl className="strat-metrics">
          <div><dt>Trades</dt><dd>{record.trades ?? "—"}</dd></div>
          <div><dt>Win rate</dt><dd>{pct(record.win_rate)}</dd></div>
          <div><dt>Max drawdown</dt><dd className="neg">{pct(record.max_drawdown_pct, 1)}</dd></div>
        </dl>
        <p className="strat-caption">
          Backtest on {record.symbol || "its universe"}, {record.timeframe} window, {record.first_date} to {record.last_date}
        </p>
      </>
    );
  else if (record.source === "paper" || record.source === "sessions") {
    const other =
      record.source === "sessions"
        ? [record.skipped && `${record.skipped} skipped, missed or failed without P&L`, record.killed && `${record.killed} killed or errored`, record.open && `${record.open} in progress`].filter(Boolean).join(" · ")
        : "";
    body = (
      <>
        <dl className="strat-metrics">
          <div><dt>Sessions</dt><dd>{record.sessions}</dd></div>
          <div><dt>Win rate</dt><dd>{pct(record.win_rate)}</dd></div>
          <div><dt>Max drawdown</dt><dd className={record.max_drawdown < 0 ? "neg" : ""}>{signedUsd(record.max_drawdown)}</dd></div>
        </dl>
        <p className="strat-caption">
          {record.sessions ? `Net ${signedUsd(record.net_pnl)}, ${record.first_date} to ${record.last_date}` : "No settled sessions yet"}
          {other ? ` · ${other}` : ""}
        </p>
        <p className="strat-caption">
          {record.source === "sessions"
            ? "Realized P&L per session, including unwind losses. Not a backtest."
            : "From the app's trade ledger. Not a backtest."}
        </p>
      </>
    );
  } else if (record.source === "external") body = <p className="muted">{record.note}</p>;
  else body = <p className="muted">No results yet. Simulated sessions appear here after the first closed trade.</p>;
  return (
    <section className="card strat-research" aria-label={`Research results for ${strategy.name}`}>
      <h2>Track record</h2>
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
  const [selDetail, setSelDetail] = useState(null);
  const [reloads, setReloads] = useState(0);

  const load = () => {
    setLoading(true);
    return api
      .strategies()
      .then((d) => {
        const list = d.strategies || [];
        setStrategies(list);
        setSessionDate(d.session_date || null);
        setError(null);
        setReloads((n) => n + 1);
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
    setSelDetail(null);
    api
      .strategyRecord(selectedKey)
      .then((r) => alive && setRecord(r))
      .catch((e) => alive && setRecordError(e.status === 404 ? "this server does not provide it yet" : e.message));
    api
      .strategy(selectedKey)
      .then((d) => alive && setSelDetail(d))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [selectedKey, reloads]);

  if (detailKey)
    return (
      <StrategyDetail
        key={detailKey}
        keyName={detailKey}
        summary={strategies.find((s) => s.key === detailKey) || null}
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
  const selLive = selected && selDetail?.overview?.key === selected.key ? selDetail : null;
  const selSchedule = selLive ? scheduleOf(selLive.overview) : {};

  const toggle = async (s) => {
    setBusy(true);
    setActionMsg(null);
    try {
      await (s.running ? api.stop(s.key) : api.activate(s.key));
      setActionMsg({ ok: true, text: `${s.name} ${s.running ? "paused" : "started"}` });
      await load();
    } catch (e) {
      setActionMsg({
        ok: false,
        text: e.status === 401 ? "Starting or pausing needs the app access token (Connections)" : `Could not ${s.running ? "pause" : "start"} ${s.name}: ${e.message}`,
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
  const dash = loading || error;

  return (
    <div className="strategies-tab">
      <header className="strat-head">
        <div>
          <h1>Strategies</h1>
          <p className="strat-lede">Rules, allocation and today's result for each strategy</p>
        </div>
        <div className="strat-session">
          <span className="strat-date">{sessionDateLabel(sessionDate)} · New York session</span>
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

      <dl className="strat-figures">
        <div>
          <dt>Running</dt>
          <dd>{dash ? "—" : running.length}{!dash && <small>of {ready.length}</small>}</dd>
        </div>
        <div>
          <dt>Allocated capital</dt>
          <dd>{dash ? "—" : usd(allocated)}</dd>
          <p>Across running strategies</p>
        </div>
        <div>
          <dt>Session P&amp;L</dt>
          <dd className={tone(sessionPnl)}>{dash ? "—" : signedUsd(sessionPnl)}</dd>
          <p>Realized today, app-tracked</p>
        </div>
        <div>
          <dt>Execution</dt>
          <dd>Paper</dd>
          <p>{executions.length ? executions.join(" · ") : "See Connections for routing"}</p>
        </div>
      </dl>

      <section className="card strat-list" aria-label="Strategies">
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
                  <th className="col-universe">Universe</th>
                  <th className="num">Allocation</th>
                  <th className="num">Today</th>
                  <th className="num col-month">Month</th>
                  <th className="num">Year</th>
                  <th>Status</th>
                  <th aria-label="Action" />
                </tr>
              </thead>
              <tbody>
                {ready.map((s) => {
                  const st = strategyStatus(s);
                  return (
                    <tr key={s.key} className={s.key === selectedKey ? "selected" : ""} onClick={() => setSelectedKey(s.key)}>
                      <td className="strat-name-cell">
                        <button
                          className="strat-name"
                          aria-pressed={s.key === selectedKey}
                          onClick={(e) => {
                            e.stopPropagation();
                            setSelectedKey(s.key);
                          }}
                        >
                          {s.name}
                        </button>
                        {s.category && <div className="strat-sub">{s.category}</div>}
                      </td>
                      <td className="strat-mono col-universe">{universeOf(s)}</td>
                      <td className="num">{usd(s.capital_allocation || 0)}</td>
                      <td
                        className={"num " + tone(s.session_pnl)}
                        title={s.session_pnl == null ? (s.external_runner ? "Tracked by the external runner, not this app" : "Not reported by this server yet") : undefined}
                      >
                        {signedUsd(s.session_pnl)}
                      </td>
                      <td className={"num col-month " + tone(s.month_pnl)}>{signedUsd(s.month_pnl)}</td>
                      <td className={"num " + tone(s.year_pnl)}>{signedUsd(s.year_pnl)}</td>
                      <td><StatusMark s={s} /></td>
                      <td className="strat-action" onClick={(e) => e.stopPropagation()}>
                        {canToggle(s) ? (
                          <ToggleButton {...toggleProps(s, false)} />
                        ) : (
                          <span className="muted small" title={st.title}>{s.external_runner ? "Runs externally" : "Unavailable"}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {placeholders.length > 0 && (
          <p className="strat-note">
            {placeholders.length} more in development: {placeholders.map((s) => s.name).join(", ")}
          </p>
        )}
      </section>

      {selected && (
        <div className="strat-bottom">
          <section className="card strat-selected" aria-label={`${selected.name} summary`}>
            <div className="strat-selected-head">
              <div>
                <h2>{selected.name}</h2>
                {selected.category && <div className="strat-sub">{selected.category}</div>}
              </div>
              <StatusMark s={selected} />
            </div>
            {selected.description && <p className="strat-desc">{selected.description}</p>}
            <SessionRail entry={selSchedule.entry} exit={selSchedule.exit} compact />
            <dl className="strat-facts">
              <div><dt>Universe</dt><dd className="strat-mono">{universeOf(selected)}</dd></div>
              <div><dt>Allocation</dt><dd>{usd(selected.capital_allocation || 0)}</dd></div>
              <div><dt>Execution</dt><dd>{selected.execution || "—"}</dd></div>
              <div><dt>Phase</dt><dd>{selLive ? humanize(selLive.phase || selLive.status) : "—"}</dd></div>
            </dl>
            {!canToggle(selected) && strategyStatus(selected).title && <p className="muted small">{strategyStatus(selected).title}</p>}
            <div className="strat-actions">
              <ToggleButton {...toggleProps(selected, true)} />
              <button className="btn" onClick={() => setDetailKey(selected.key)}>Open details</button>
            </div>
          </section>
          <ResearchSnapshot strategy={selected} record={record} error={recordError} />
        </div>
      )}
    </div>
  );
}
