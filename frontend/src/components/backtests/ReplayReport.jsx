import { useId, useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { humanize, pct, signedUsd, tone, usd } from "../strategyUi.jsx";
import { replaySeries } from "./replaySeries.js";
import "./replayReport.css";

const PAGE_SIZE = 10;
const startPoint = { date: "Before first session", cumulative: 0 };
const axisUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const sessionDate = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const chartDate = (date) => date === startPoint.date ? "Start" : sessionDate.format(new Date(date + "T12:00:00Z"));

function CurveTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <div className="replay-tooltip">
      <span>{row.date === startPoint.date ? row.date : chartDate(row.date)}</span>
      <strong>{signedUsd(row.cumulative)}</strong>
    </div>
  );
}

export function PerformanceCurve({ rows }) {
  const titleId = useId();
  const data = useMemo(() => [startPoint, ...rows], [rows]);
  return (
    <figure className="replay-curve" aria-labelledby={titleId}>
      <figcaption id={titleId}>Cumulative net P&amp;L</figcaption>
      {rows.some((row) => row.traded) ? (
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={data} accessibilityLayer aria-label="Cumulative net profit and loss in dollars by session"
            margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--border-soft)" />
            <XAxis dataKey="date" tickFormatter={chartDate} tick={{ fontSize: 11, fill: "var(--muted)" }}
              minTickGap={35} tickMargin={8} tickLine={false} axisLine={false} />
            <YAxis domain={["auto", "auto"]} tickFormatter={(n) => axisUsd.format(n)}
              tick={{ fontSize: 11, fill: "var(--muted)" }} width={55} tickLine={false} axisLine={false} />
            <Tooltip content={<CurveTooltip />} cursor={{ stroke: "var(--faint)", strokeDasharray: "3 3" }} />
            <ReferenceLine y={0} stroke="var(--faint)" strokeDasharray="4 4" />
            <Area type="linear" dataKey="cumulative" name="Cumulative net P&L" stroke="var(--accent)"
              fill="var(--accent)" fillOpacity={0.08} strokeWidth={1.8} activeDot={{ r: 3 }} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      ) : <p className="muted">No traded sessions in this replay.</p>}
    </figure>
  );
}

function SessionResults({ rows, incomplete, hasUnresolved }) {
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const newestFirst = useMemo(() => [...rows].reverse(), [rows]);
  const visible = newestFirst.filter((row) => {
    if (filter === "traded") return row.traded;
    if (filter === "unresolved") return row.status === "unresolved";
    if (filter === "untraded") return !row.traded && row.status !== "unresolved";
    return true;
  });
  const pageCount = Math.ceil(visible.length / PAGE_SIZE);
  const start = page * PAGE_SIZE;
  const end = Math.min(start + PAGE_SIZE, visible.length);
  return (
    <section className="replay-history" aria-label="Replay sessions">
      <div className="replay-history-head">
        <h3>Session results</h3>
        <select aria-label="Filter session results by outcome" value={filter} onChange={(e) => { setFilter(e.target.value); setPage(0); }}>
          <option value="all">All outcomes</option>
          <option value="traded">Completed sessions</option>
          {hasUnresolved && <option value="unresolved">Unresolved sessions</option>}
          <option value="untraded">Untraded sessions</option>
        </select>
      </div>
      {visible.length ? (
        <>
          <div className="table-scroll replay-scroll" role="region" tabIndex={0} aria-label="Session results table">
            <table className="table">
              <thead><tr>
                <th scope="col">Session date</th><th scope="col" className="num">Net P&amp;L</th>
                <th scope="col">Outcome</th>{!incomplete && <th scope="col" className="num">Cumulative P&amp;L</th>}
                <th scope="col">Short-leg stops</th>
              </tr></thead>
              <tbody>
                {visible.slice(start, end).map((row) => (
                  <tr key={row.date}>
                    <td><time dateTime={row.date}>{chartDate(row.date)}</time></td>
                    <td className={"num " + (row.traded ? tone(row.pnl) : "")}>{row.traded ? signedUsd(row.pnl) : "—"}</td>
                    <td>
                      {row.status === "skipped" ? (row.data_gap ? "Data gap" : "Rule skip") : humanize(row.status)}
                      {row.reason && <small className="replay-reason">{row.reason}</small>}
                    </td>
                    {!incomplete && <td className="num">{signedUsd(row.cumulative)}</td>}
                    <td>{row.stops?.length ? row.stops.map(humanize).join(", ") : (row.traded ? "None" : "—")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="replay-pagination">
            <span role="status">Sessions {start + 1}–{end} of {visible.length}</span>
            {pageCount > 1 && (
              <nav aria-label="Session result pages">
                <button type="button" className="btn ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
                <button type="button" className="btn ghost" disabled={page === pageCount - 1} onClick={() => setPage(page + 1)}>Next</button>
              </nav>
            )}
          </div>
        </>
      ) : <p className="muted" role="status">No sessions match this outcome.</p>}
    </section>
  );
}

export default function ReplayReport({ report }) {
  const widths = Object.keys(report.by_width || {}).sort((a, b) => Number(a) - Number(b));
  const [selectedWidth, setSelectedWidth] = useState(() => widths[0]);
  const scenario = report.by_width?.[selectedWidth];
  const rows = useMemo(() => replaySeries(scenario?.sessions), [scenario]);
  if (!scenario?.aggregate) return <p className="muted">No scenarios were recorded for this replay.</p>;
  const summary = scenario.aggregate;
  const spy = report.params?.ticker === "SPY";
  const unresolved = rows.filter((row) => row.status === "unresolved").length;
  const entryGaps = spy ? rows.filter((row) => row.status === "skipped" && row.data_gap).length : 0;
  const incomplete = unresolved > 0 || entryGaps > 0;
  return (
    <div className="replay-report">
      <div className="replay-scenario">
        <span>Modeled bid/ask width</span>
        {widths.length === 1 ? <b>{Number(selectedWidth)} pts</b> : (
          <div className="replay-widths" role="group" aria-label="Modeled bid/ask width per leg">
            {widths.map((key) => (
              <button type="button" key={key} aria-pressed={selectedWidth === key} onClick={() => setSelectedWidth(key)}>
                {Number(key).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 })} pts
              </button>
            ))}
          </div>
        )}
        <p>
          The replay uses historical option trades as midpoints and applies the selected spread to each leg.
          Each session starts with the same sizing capital and a cleared daily-loss halt.
          {spy && " SPY closing orders start 30 minutes before the equity session ends."}
        </p>
      </div>
      {incomplete && (
        <p className="sd-outcome warn">
          {unresolved > 0 && <b>{unresolved} of {summary.sessions} sessions unresolved. </b>}
          {entryGaps > 0 && <b>{entryGaps} {entryGaps === 1 ? "session lacked" : "sessions lacked"} entry data. </b>}
          Full-period P&amp;L, win rate, and drawdown are unknown.
        </p>
      )}
      <dl className="replay-metrics">
        <div><dt>{incomplete ? "Closed-session P&L" : "Net P&L"}</dt><dd className={tone(summary.total_pnl)}>{signedUsd(summary.total_pnl)}</dd></div>
        <div><dt>Win rate</dt><dd>{pct(incomplete ? null : summary.win_rate, 1)}</dd></div>
        <div><dt>Max drawdown</dt><dd className={tone(incomplete ? null : summary.max_drawdown)}>{signedUsd(incomplete ? null : summary.max_drawdown)}</dd></div>
        <div><dt>{spy ? "Completed sessions" : "Traded sessions"}</dt><dd>{summary.traded ?? "—"} <small>of {summary.sessions ?? "—"}</small></dd></div>
      </dl>
      {!incomplete && <PerformanceCurve rows={rows} />}
      {(!incomplete || summary.traded > 0) && (
        <div className="replay-facts">
          {!incomplete && <>
            <span>Best session <b className={tone(summary.best_day)}>{signedUsd(summary.best_day)}</b></span>
            <span>Worst session <b className={tone(summary.worst_day)}>{signedUsd(summary.worst_day)}</b></span>
          </>}
          <span>{incomplete ? "Fees on closed sessions" : "Fees included"} <b>{usd(summary.fees)}</b></span>
        </div>
      )}
      <SessionResults key={selectedWidth} rows={rows} incomplete={incomplete} hasUnresolved={unresolved > 0} />
    </div>
  );
}
