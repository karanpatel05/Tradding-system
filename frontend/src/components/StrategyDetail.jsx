import { useEffect, useState } from "react";
import { api } from "../api.js";
import BacktestResults from "./backtests/BacktestResults.jsx";
import StrategyPnl from "./StrategyPnl.jsx";
import {
  usd,
  signedUsd,
  tone,
  humanize,
  isOptions,
  canToggle,
  strategyStatus,
  StatusMark,
  ToggleButton,
  SessionRail,
  scheduleOf,
  ChevronLeft,
} from "./strategyUi.jsx";

const timeOf = (iso) =>
  iso ? new Date(iso).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "America/New_York" }) : "—";
const dayLabel = (iso) => (iso ? new Date(iso + "T12:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "—");
const nyToday = () => new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
const SESSION_TONE = { closed: "", open: "live", entering: "live", missed: "warn", skipped: "", failed: "bad", rejected: "bad", abandoned: "bad", interrupted: "bad" };

function fmt(v) {
  return v === null || v === undefined || v === "" ? "—" : String(v);
}

function BackLink({ onBack }) {
  return (
    <button className="strat-back" onClick={onBack}>
      <ChevronLeft />
      Strategies
    </button>
  );
}

export default function StrategyDetail({ keyName, summary, onBack, initialView = "overview" }) {
  const [detail, setDetail] = useState(null);
  const [view, setView] = useState(initialView);
  const [sessions, setSessions] = useState(null);
  const [alloc, setAlloc] = useState("");
  const [msg, setMsg] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api
      .strategy(keyName)
      .then((d) => {
        setDetail(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(e.message));
  useEffect(() => {
    load();
  }, [keyName]);

  const ready = detail?.overview?.ready;

  useEffect(() => {
    let alive = true;
    if (ready && view === "overview") {
      api
        .strategySessions(keyName)
        .then((d) => alive && setSessions(d?.sessions || []))
        .catch(() => alive && setSessions([]));
    }
    return () => {
      alive = false;
    };
  }, [ready, keyName, view]);

  const act = async (fn, verb) => {
    setMsg(null);
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (err) {
      setMsg(err.status === 401 ? `${verb} needs the app access token (Connections)` : err.message);
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  if (!detail)
    return (
      <div className="strategy-detail">
        <BackLink onBack={onBack} />
        <div className={loadError ? "banner error" : "empty"} role="status">
          {loadError ? `Could not load strategy: ${loadError}` : "Loading strategy…"}
        </div>
      </div>
    );

  const ov = detail.overview;
  const s = {
    key: keyName,
    name: ov.name,
    ready: ov.ready,
    running: detail.running,
    status: detail.status,
    archived: detail.archived,
    external_runner: detail.external_runner,
    activation_blocked: detail.activation_blocked,
  };
  const overview = view === "overview";
  const meta = [summary?.category, summary?.universe?.join(", "), overview && summary?.execution].filter(Boolean);
  const { entry, exit } = scheduleOf(ov);
  const today = nyToday();
  const todaySession = sessions?.find((x) => x.session_date === today);
  const pos = detail.position;

  return (
    <div className="strategy-detail">
      <BackLink onBack={onBack} />
      <header className="sd-head">
        <div className="sd-title">
          <h1>{ov.name}</h1>
          {meta.length > 0 && <p className="sd-meta">{meta.join(" · ")}</p>}
        </div>
        {overview && (
          <div className="sd-head-actions">
            <StatusMark s={s} />
            {ready && !detail.external_runner && (
              <ToggleButton
                s={s}
                primary
                busy={busy}
                confirming={confirming}
                onAsk={() => setConfirming(true)}
                onCancel={() => setConfirming(false)}
                onConfirm={() => act(() => (s.running ? api.stop(keyName) : api.activate(keyName)), s.running ? "Pausing" : "Starting")}
              />
            )}
          </div>
        )}
      </header>
      {ready && (
        <div className="sd-view-switch" role="group" aria-label="Strategy view">
          <button aria-pressed={overview} onClick={() => setView("overview")}>Overview</button>
          <button aria-pressed={!overview} onClick={() => setView("backtests")}>Backtest results</button>
        </div>
      )}
      {overview && ready && ov.description && <p className="sd-lede">{ov.description}</p>}
      {overview && msg && <div className="banner error" role="alert">{msg}</div>}
      {overview && ready && !canToggle(s) && !detail.external_runner && strategyStatus(s).title && <p className="muted small">{strategyStatus(s).title}</p>}

      {!ready ? (
        <section className="card sd-soon">
          <h2>Coming soon</h2>
          <p className="muted">
            The rules for this strategy are not implemented yet. The overview and backtest appear here, and activation and allocation unlock, once
            they are.
          </p>
        </section>
      ) : !overview ? (
        <BacktestResults keyName={keyName} strategyName={ov.name} />
      ) : (
        <div className={detail.archived ? "sd-archive" : "sd-grid"}>
          <div className="sd-main">
            <section className="card sd-today" aria-label="Today">
              <div className="sd-card-head">
                <h2>Today</h2>
                <span className="sd-card-meta">{dayLabel(today)}</span>
              </div>
              <SessionRail entry={entry} exit={exit} />
              <dl className="sd-facts">
                <div><dt>Phase</dt><dd>{humanize(detail.phase || detail.status)}</dd></div>
                <div><dt>Trades today</dt><dd>{fmt(detail.trades_today)}</dd></div>
                <div>
                  <dt>Position</dt>
                  <dd>{pos ? `${humanize(pos.side)} ${fmt(pos.quantity)} ${fmt(pos.symbol)} @ ${fmt(pos.entry_price)}` : "Flat"}</dd>
                </div>
                <div><dt>Last signal</dt><dd>{timeOf(detail.last_signal_time)}</dd></div>
              </dl>
              {todaySession && (
                <p className={"sd-outcome " + (SESSION_TONE[todaySession.status] || "")}>
                  <b>{humanize(todaySession.status)}</b>
                  {todaySession.reason ? ` · ${todaySession.reason}` : ""}
                  {todaySession.realized_pnl != null ? ` · ${signedUsd(todaySession.realized_pnl)}` : ""}
                </p>
              )}
            </section>

            <StrategyPnl keyName={keyName} />

            <section className="card sd-rules" aria-label="How it trades">
              <h2>How it trades</h2>
              <dl className="sd-logic">
                <div><dt>Entry</dt><dd>{fmt(ov.entry_logic)}</dd></div>
                <div><dt>Exit</dt><dd>{fmt(ov.exit_logic)}</dd></div>
                <div><dt>Selection</dt><dd>{fmt(ov.selection_criteria)}</dd></div>
              </dl>
              {ov.rules?.length > 0 && (
                <>
                  <h3>Rules</h3>
                  <ol className="sd-rule-list">
                    {ov.rules.map((r, i) => (
                      <li key={i}>
                        <span className="sd-rule-n">{String(i + 1).padStart(2, "0")}</span>
                        <span>{r}</span>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </section>

            {sessions?.length > 0 && (
              <section className="card sd-sessions" aria-label="Sessions">
                <div className="sd-card-head">
                  <h2>Sessions</h2>
                  <span className="sd-card-meta">{sessions.length} recorded</span>
                </div>
                <div className="table-scroll">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Outcome</th>
                        <th>Note</th>
                        <th className="num">Realized P&amp;L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sessions.slice(0, 20).map((x, i) => (
                        <tr key={x.id ?? `${x.session_date}-${i}`}>
                          <td className="sd-date">{dayLabel(x.session_date)}</td>
                          <td><span className={"sd-dot " + (SESSION_TONE[x.status] || "")}><i aria-hidden="true" />{humanize(x.status)}</span></td>
                          <td className="sd-note">{x.reason || "—"}</td>
                          <td className={"num " + tone(x.realized_pnl)}>{signedUsd(x.realized_pnl)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
          </div>

          {!detail.archived && <aside className="sd-side">
            {detail.external_runner ? (
              <ExternalRunnerCard detail={detail} />
            ) : (
              <section className="card sd-controls" aria-label="Allocation">
                <h2>Allocation</h2>
                <div className="sd-alloc-now">
                  <span>Current</span>
                  <b>{usd(detail.capital_allocation || 0)}</b>
                </div>
                <div className="sd-alloc-form">
                  <input
                    className="input"
                    type="number"
                    placeholder="Amount in $"
                    aria-label="Capital allocation in dollars"
                    value={alloc}
                    onChange={(e) => setAlloc(e.target.value)}
                  />
                  <button className="btn" disabled={busy || alloc === ""} onClick={() => act(() => api.allocate(keyName, Number(alloc)), "Setting allocation")}>
                    Set allocation
                  </button>
                </div>
                <dl className="sd-kv">
                  <div><dt>Execution</dt><dd>{summary?.execution || "—"}</dd></div>
                  <div><dt>Contracts cap</dt><dd>{fmt(detail.contracts)}</dd></div>
                  <div><dt>Broker</dt><dd>{detail.broker_connected ? "Connected" : "Disconnected"}</dd></div>
                </dl>
                {summary && !isOptions(summary) && (
                  <p className="strat-caption">
                    Each trade is sized into whole shares from this allocation and the per-trade risk %, capped by allocation ÷ entry price. Orders go to
                    Alpaca paper only when <code>ENABLE_LIVE_ORDERS</code> is set server-side, otherwise they are simulated in the app.
                  </p>
                )}
              </section>
            )}
          </aside>}
        </div>
      )}
    </div>
  );
}

// A strategy that executes outside this app (e.g. the QQQ credit spread under its
// own LaunchAgent). Read-only: shows the in-app switch as Off with the server's
// reason, plus today's state read from the external runner. No activate/allocate.
function ExternalRunnerCard({ detail }) {
  const ext = detail.external || {};
  const today = ext.today;
  const pos = today?.position;
  return (
    <section className="card sd-controls" aria-label="External runner">
      <h2>External runner</h2>
      <label className="sd-switch" title={detail.activation_blocked || ""}>
        <input type="checkbox" role="switch" checked={false} disabled aria-label="Run in this app" />
        <span>Run in this app: <b>Off</b></span>
      </label>
      {detail.activation_blocked && <p className="strat-caption">{detail.activation_blocked}</p>}
      <p className="strat-caption">The app's emergency stop does not stop the external runner.</p>
      {!ext.found ? (
        <p className="strat-caption">Runner folder not found on this host ({fmt(ext.live_dir)})</p>
      ) : (
        <dl className="sd-kv">
          <div>
            <dt>Service</dt>
            <dd>
              <span className={"sd-dot " + (ext.daemon_running ? "live" : "")}><i aria-hidden="true" />{ext.daemon_running ? "Running" : "Not running"}</span>
              {ext.pid ? <span className="muted small"> · pid {ext.pid}</span> : null}
            </dd>
          </div>
          <div><dt>Runner</dt><dd><code>{fmt(ext.runner)}</code></dd></div>
          <div><dt>Session</dt><dd>{fmt(today?.session_date)}</dd></div>
          <div><dt>Entry</dt><dd>{fmt(today?.entry)}</dd></div>
          <div><dt>15:30 check</dt><dd>{fmt(today?.checkpoint)}</dd></div>
          <div><dt>Final close</dt><dd>{fmt(today?.final_close)}</dd></div>
          <div>
            <dt>Today's spread</dt>
            <dd>
              {pos
                ? `${pos.ticker} ${pos.spread} ${fmt(pos.short_strike)}/${fmt(pos.long_strike)} ×${fmt(pos.quantity)} @ ${fmt(pos.credit_per_share)} credit (${fmt(pos.status)})`
                : "—"}
            </dd>
          </div>
        </dl>
      )}
    </section>
  );
}
