import { useEffect, useState } from "react";
import { api } from "../../api.js";
import ReplayReport from "./ReplayReport.jsx";
import ResearchReport, { ResearchPeriodReport } from "./ResearchReport.jsx";
import SavedBacktest from "./SavedBacktest.jsx";

const dateFormat = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const displayDate = (date) => dateFormat.format(new Date(date + "T12:00:00Z"));

function LoadError({ message, retry }) {
  return (
    <div className="bt-load-error">
      <p role="alert">Could not load backtest results. {message}</p>
      <button className="btn tiny" onClick={retry}>Try again</button>
    </div>
  );
}

function Replay({ strategyKey, id }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    api.backtestReport(strategyKey, id)
      .then((data) => { if (active) setReport(data); })
      .catch((err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [strategyKey, id, attempt]);

  if (error) return <LoadError message={error} retry={() => { setError(""); setAttempt((n) => n + 1); }} />;
  if (!report) return <p className="bt-status" role="status">Loading saved report…</p>;
  if (report.kind === "research-period") return <ResearchPeriodReport report={report} />;
  if (report.kind === "research") return <ResearchReport report={report} />;
  return <ReplayReport report={report} />;
}

function ReplayLibrary({ strategyKey, reports }) {
  const [selectedId, setSelectedId] = useState(reports[0].id);
  const selected = reports.find((item) => item.id === selectedId);
  return (
    <>
      <div className="bt-report-head">
        <div>
          <h2>{selected.kind === "research" || selected.kind === "research-period" ? "Research backtest" : "Historical replay"}</h2>
          <p className="sd-card-meta">{displayDate(selected.start)} – {displayDate(selected.end)}</p>
        </div>
        <label className="bt-report-select">
          Saved report
          <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
            {reports.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
      </div>
      <Replay key={selectedId} strategyKey={strategyKey} id={selectedId} />
    </>
  );
}

// Older servers expose the last equity result but have no replay catalogue.
async function loadSaved(strategyKey) {
  let catalog;
  try {
    catalog = await api.backtestReports(strategyKey);
  } catch (error) {
    if (error.status !== 404) throw error;
    catalog = { reports: [], can_run: false, legacy: true };
  }
  if (!catalog.reports.length && (catalog.can_run || catalog.legacy)) {
    return { ...catalog, last: await api.backtestLast(strategyKey) };
  }
  return catalog;
}

function EquityBacktest({ catalog, strategyKey, strategyName }) {
  const [result, setResult] = useState(catalog.last);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState("");

  async function runBacktest() {
    setRunning(true);
    setMessage("");
    try {
      const run = await api.backtest(strategyKey);
      if (run.data_available) setResult(run);
      else setMessage(run.note || "This run returned no results.");
    } catch (err) {
      setMessage(`Backtest failed. ${err.message}`);
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <div className="bt-report-head">
        <h2>Saved backtest</h2>
        {catalog.can_run && (
          <button className="btn" onClick={runBacktest} disabled={running}>
            {running ? "Running…" : "Run backtest"}
          </button>
        )}
      </div>
      {message && <p className="bt-status" role="status">{message}</p>}
      {result?.data_available ? <SavedBacktest result={result} /> : (
        <p className="bt-status">{catalog.legacy
          ? "This server has no saved equity result and does not expose historical replay reports."
          : `No backtest results have been saved for ${strategyName} on this server.`}</p>
      )}
    </>
  );
}

export default function BacktestResults({ keyName, strategyName }) {
  const [catalog, setCatalog] = useState(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    loadSaved(keyName)
      .then((data) => { if (active) setCatalog(data); })
      .catch((err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [keyName, attempt]);

  let body;
  if (error) {
    body = <LoadError message={error} retry={() => { setError(""); setAttempt((n) => n + 1); }} />;
  } else if (!catalog) {
    body = <p className="bt-status" role="status">Loading saved backtests…</p>;
  } else if (catalog.reports.length) {
    body = <ReplayLibrary strategyKey={keyName} reports={catalog.reports} />;
  } else {
    body = <EquityBacktest catalog={catalog} strategyKey={keyName} strategyName={strategyName} />;
  }
  return <section className="card bt-results" aria-label={`Backtest results for ${strategyName}`}>{body}</section>;
}
