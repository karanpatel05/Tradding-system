import { useState } from "react";
import { api } from "../../api.js";
import { tone } from "../strategyUi.jsx";
import "./researchReport.css";

const modeNames = {
  hold: "Hold to expiration",
  stop125: "1.25× stop per short leg",
  stop150: "1.5× stop per short leg",
  stop200: "2× stop per short leg",
  stopbe: "1.5× stop, then breakeven",
  stopt125: "1.5× stop, then 1.25×",
  stopl200: "1.5× stop, then 2×",
  exitall: "Close the fly at the first 1.5× stop",
  stopvert: "Close each vertical at its 1.5× stop",
};

const signedMoney = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 2, signDisplay: "exceptZero",
});

function ScenarioResults({ scenarios, daysTraded }) {
  const [wing, setWing] = useState(10);
  const [exit, setExit] = useState("stop150");
  const [spread, setSpread] = useState(0.01);
  const wings = [...new Set(scenarios.map((row) => row.wing_width))].sort((a, b) => a - b);
  const wingRows = scenarios.filter((row) => row.wing_width === (wings.includes(wing) ? wing : wings[0]));
  const exits = [...new Set(wingRows.map((row) => row.mode))];
  const exitRows = wingRows.filter((row) => row.mode === (exits.includes(exit) ? exit : exits[0]));
  const spreads = exitRows.map((row) => row.half_spread).sort((a, b) => a - b);
  const row = exitRows.find((r) => r.half_spread === spread) || exitRows[0];

  return (
    <>
      <div className="research-controls">
        <label className="bt-report-select">Wing width
          <select value={row.wing_width} onChange={(e) => setWing(Number(e.target.value))}>
            {wings.map((w) => <option key={w} value={w}>${w}</option>)}
          </select>
        </label>
        <label className="bt-report-select">Exit rule
          <select value={row.mode} onChange={(e) => setExit(e.target.value)}>
            {exits.map((mode) => <option key={mode} value={mode}>{modeNames[mode] ?? mode}</option>)}
          </select>
        </label>
        <label className="bt-report-select">Half-spread
          <select value={row.half_spread} onChange={(e) => setSpread(Number(e.target.value))}>
            {spreads.map((s) => <option key={s} value={s}>${s.toFixed(2)} per side (${(s * 2).toFixed(2)} full width)</option>)}
          </select>
        </label>
      </div>
      <dl className="replay-metrics">
        <div><dt>Net P&amp;L</dt><dd className={tone(row.total_pnl)}>{signedMoney.format(row.total_pnl)}</dd></div>
        <div><dt>Win rate</dt><dd>{row.win_rate_pct.toFixed(1)}%</dd></div>
        <div><dt>Max drawdown</dt><dd className={tone(row.max_drawdown)}>{signedMoney.format(row.max_drawdown)}</dd></div>
        <div><dt>Traded sessions</dt><dd>{daysTraded}</dd></div>
      </dl>
      <div className="replay-facts">
        <span>Average day <b>{signedMoney.format(row.avg_day)}</b></span>
        <span>Best day <b>{signedMoney.format(row.best_day)}</b></span>
        <span>Worst day <b>{signedMoney.format(row.worst_day)}</b></span>
        <span>Annualized Sharpe <b>{row.sharpe == null ? "—" : row.sharpe.toFixed(2)}</b></span>
      </div>
    </>
  );
}

export default function ResearchReport({ report }) {
  return (
    <div className="replay-report research-report">
      <p className="research-method">
        The SPY0DTEIronFly study enters one iron fly at 9:45 a.m. ET. It models fills around VWAP and
        charges $0.035 per contract. Missing wing prices use Black–Scholes estimates.
        Legs held to expiration are valued at intrinsic value.
      </p>
      {report.scenarios.length > 0 ? (
        <ScenarioResults scenarios={report.scenarios} daysTraded={report.days_traded} />
      ) : <p className="bt-status">No scenarios were saved in the research summary.</p>}
      {report.downloads.length > 0 && (
        <section aria-label="Research files" className="research-files">
          <h3>Source files</h3>
          <ul>
            {report.downloads.map((file) => (
              <li key={file.filename}><a href={`${api.base}${file.url}`} download={file.filename}>{file.label}</a></li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
