// Realized P&L for one strategy: the year's total, twelve monthly bars and the
// selected month as a weekday calendar. Data: GET /api/strategies/{key}/pnl.
import { useEffect, useState } from "react";
import { api } from "../api.js";
import { signedUsd, tone, humanize } from "./strategyUi.jsx";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"];

const compactUsd = (n) => {
  if (n == null) return "—";
  const a = Math.abs(n);
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  if (a >= 1000) return `${sign}$${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}k`;
  return `${sign}$${Math.round(a)}`;
};

// Weeks (Mon–Fri) covering one month; null marks a weekday outside it.
function weekdayGrid(year, month) {
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const weeks = [];
  let week = [null, null, null, null, null];
  for (let d = 1; d <= days; d++) {
    const dow = new Date(Date.UTC(year, month - 1, d)).getUTCDay(); // 0 Sun … 6 Sat
    if (dow === 0 || dow === 6) continue;
    week[dow - 1] = d;
    if (dow === 5) {
      weeks.push(week);
      week = [null, null, null, null, null];
    }
  }
  if (week.some((x) => x != null)) weeks.push(week);
  return weeks;
}

const iso = (y, m, d) => `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
const nyToday = () => new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());

export default function StrategyPnl({ keyName }) {
  const today = nyToday();
  const thisYear = Number(today.slice(0, 4));
  const [year, setYear] = useState(thisYear);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [month, setMonth] = useState(Number(today.slice(5, 7)));

  useEffect(() => {
    let alive = true;
    setError(null);
    api
      .strategyPnl(keyName, year)
      .then((d) => {
        if (!alive) return;
        setData(d);
        // Land on the current month this year; otherwise the last month with sessions.
        if (d.year === thisYear) setMonth(Number(today.slice(5, 7)));
        else {
          const last = [...(d.months || [])].reverse().find((m) => m.sessions > 0);
          setMonth(last ? last.month : 12);
        }
      })
      .catch((e) => alive && setError(e.status === 404 ? "This server does not provide P&L history yet" : e.message));
    return () => {
      alive = false;
    };
  }, [keyName, year]);

  if (data?.source === "external")
    return (
      <section className="card pnl" aria-label="Realized P&L">
        <h2>Realized P&amp;L</h2>
        <p className="muted">{data.note}</p>
      </section>
    );

  const years = data?.years?.length ? data.years : [thisYear];
  const minYear = Math.min(...years);
  const maxYear = Math.max(...years, thisYear);
  const months = data?.months || [];
  const maxAbs = Math.max(1, ...months.map((m) => Math.abs(m.pnl)));
  const sel = months.find((m) => m.month === month) || { month, pnl: 0, sessions: 0, wins: 0 };
  const byDate = Object.fromEntries((data?.days || []).map((r) => [r.date, r]));
  const monthDays = (data?.days || []).filter((r) => Number(r.date.slice(5, 7)) === month && r.pnl != null);
  const dayMax = Math.max(1, ...monthDays.map((r) => Math.abs(r.pnl)));
  const loading = !data || data.year !== year;

  return (
    <section className="card pnl" aria-label="Realized P&L">
      <div className="pnl-head">
        <h2>Realized P&amp;L</h2>
        <div className="pnl-year" role="group" aria-label="Year">
          <button className="pnl-step" onClick={() => setYear(year - 1)} disabled={year <= minYear} aria-label="Previous year">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
          </button>
          <span className="pnl-year-label">{year}</span>
          <button className="pnl-step" onClick={() => setYear(year + 1)} disabled={year >= maxYear} aria-label="Next year">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
          </button>
        </div>
      </div>

      {error ? (
        <p className="muted small">P&amp;L history unavailable: {error}</p>
      ) : (
        <>
          <dl className="pnl-figures">
            <div>
              <dt>{year === thisYear ? "Year to date" : `${year} total`}</dt>
              <dd className={tone(data?.total)}>{loading ? "—" : signedUsd(data.total)}</dd>
              <p>{loading ? " " : `${data.sessions} session${data.sessions === 1 ? "" : "s"} · ${data.wins} won`}</p>
            </div>
            <div>
              <dt>{MONTH_NAMES[month - 1]}</dt>
              <dd className={tone(sel.sessions ? sel.pnl : null)}>{loading ? "—" : sel.sessions ? signedUsd(sel.pnl) : "—"}</dd>
              <p>{loading ? " " : `${sel.sessions} session${sel.sessions === 1 ? "" : "s"} · ${sel.wins} won`}</p>
            </div>
            <div>
              <dt>Today</dt>
              <dd className={tone(data?.today)}>{loading ? "—" : signedUsd(data.today)}</dd>
              <p>{byDate[today]?.partial ? "So far, position still open" : "New York session"}</p>
            </div>
          </dl>

          <div className={"pnl-bars" + (loading ? " loading" : "")} role="group" aria-label={`Monthly P&L, ${year}`}>
            {MONTHS.map((label, i) => {
              const m = months[i] || { month: i + 1, pnl: 0, sessions: 0 };
              const h = m.sessions ? Math.max(2, (Math.abs(m.pnl) / maxAbs) * 100) : 0;
              const future = year === thisYear && i + 1 > Number(today.slice(5, 7));
              return (
                <button
                  key={label}
                  className={"pnl-bar" + (m.month === month ? " on" : "") + (m.pnl < 0 ? " neg" : " pos") + (future ? " future" : "")}
                  onClick={() => setMonth(m.month)}
                  aria-pressed={m.month === month}
                  aria-label={`${MONTH_NAMES[i]}: ${m.sessions ? signedUsd(m.pnl) : "no sessions"}`}
                  title={m.sessions ? `${MONTH_NAMES[i]} ${signedUsd(m.pnl)}` : `${MONTH_NAMES[i]}, no sessions`}
                >
                  <span className="pnl-bar-up">{m.sessions > 0 && m.pnl >= 0 && <i style={{ height: `${h}%` }} />}</span>
                  <span className="pnl-bar-down">{m.sessions > 0 && m.pnl < 0 && <i style={{ height: `${h}%` }} />}</span>
                  <span className="pnl-bar-label">{label}</span>
                </button>
              );
            })}
          </div>

          <div className="pnl-cal" aria-label={`${MONTH_NAMES[month - 1]} ${year} by day`}>
            <div className="pnl-cal-row pnl-cal-head" aria-hidden="true">
              {WEEKDAYS.map((w) => (
                <span key={w}>{w}</span>
              ))}
            </div>
            {weekdayGrid(year, month).map((week, wi) => (
              <div className="pnl-cal-row" key={wi}>
                {week.map((d, di) => {
                  if (d == null) return <span key={di} className="pnl-day blank" />;
                  const key = iso(year, month, d);
                  const r = byDate[key];
                  const priced = r && r.pnl != null;
                  const alpha = priced ? 0.06 + 0.2 * (Math.abs(r.pnl) / dayMax) : 0;
                  const bg = priced ? (r.pnl >= 0 ? `rgba(20, 124, 102, ${alpha})` : `rgba(182, 63, 79, ${alpha})`) : undefined;
                  const label = priced ? signedUsd(r.pnl) : r ? humanize(r.status) : key > today ? "" : "No session";
                  return (
                    <span
                      key={di}
                      className={"pnl-day" + (key === today ? " today" : "") + (priced ? " " + tone(r.pnl) : r ? " status" : key > today ? " future" : " none")}
                      style={bg ? { background: bg } : undefined}
                      title={`${key}: ${label || "upcoming"}${r?.partial ? " (so far)" : ""}`}
                    >
                      <b>{d}</b>
                      {priced ? (
                        <>
                          <em className="full">{signedUsd(r.pnl)}</em>
                          <em className="short">{compactUsd(r.pnl)}</em>
                        </>
                      ) : (
                        <em className="word">{r ? humanize(r.status) : ""}</em>
                      )}
                    </span>
                  );
                })}
              </div>
            ))}
          </div>
          <p className="strat-caption">
            Realized P&amp;L by New York session date, from{" "}
            {data?.source === "ledger" ? "the app's closed trades" : "the strategy's session records, including unwind losses and fees"}
          </p>
        </>
      )}
    </section>
  );
}
