import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const iso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const fmt = (n) =>
  (n >= 0 ? "+" : "") +
  n.toLocaleString(undefined, { maximumFractionDigits: 0 });

export default function CalendarTab() {
  const [month, setMonth] = useState(() => {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1);
  });
  const [days, setDays] = useState({});
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detailError, setDetailError] = useState(null);

  const range = useMemo(() => {
    const start = new Date(month.getFullYear(), month.getMonth(), 1);
    const end = new Date(month.getFullYear(), month.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }, [month]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setSelected(null);
    setDetail(null);
    api
      .calendar(range.start, range.end)
      .then((d) => {
        if (!alive) return;
        const map = {};
        (d.days || []).forEach((row) => (map[row.date] = row));
        setDays(map);
      })
      .catch((e) => {
        if (alive) {
          setDays({});
          setError(e.message);
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [range.start, range.end]);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setDetailError(null);
    if (selected)
      api
        .dayDetail(selected)
        .then((d) => {
          if (alive) setDetail(d);
        })
        .catch((e) => {
          if (alive) setDetailError(e.message);
        });
    return () => {
      alive = false;
    };
  }, [selected]);

  const firstDow = month.getDay();
  const daysInMonth = new Date(
    month.getFullYear(),
    month.getMonth() + 1,
    0,
  ).getDate();
  const cells = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  const monthLabel = month.toLocaleString(undefined, {
    month: "long",
    year: "numeric",
  });
  const shift = (n) =>
    setMonth(new Date(month.getFullYear(), month.getMonth() + n, 1));

  return (
    <div className="calendar-tab">
      <div className="page-head">
        <div>
          <h1>Journal</h1>
          <p className="page-description">
            Review the session behind the numbers
          </p>
        </div>
        <div className="row gap center">
          <button
            className="btn ghost"
            aria-label="Previous month"
            onClick={() => shift(-1)}
          >
            ‹
          </button>
          <span className="month-label">{monthLabel}</span>
          <button
            className="btn ghost"
            aria-label="Next month"
            onClick={() => shift(1)}
          >
            ›
          </button>
        </div>
      </div>

      {error && (
        <div className="banner error" role="alert">
          Could not load journal: {error}
        </div>
      )}
      <div className="journal-workspace">
        <section className="card journal-calendar">
          {loading && (
            <div className="muted small" role="status">
              Loading journal…
            </div>
          )}
          <div className="calendar-grid">
            {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => (
              <div className="cal-dow" key={d}>
                {d}
              </div>
            ))}
            {cells.map((d, i) => {
              if (!d) return <div key={i} className="cal-cell empty" />;
              const dateStr = iso(
                new Date(month.getFullYear(), month.getMonth(), d),
              );
              const row = days[dateStr];
              const pnl = row?.realized_pnl ?? 0;
              const cls = !row
                ? ""
                : pnl > 0
                  ? "pos"
                  : pnl < 0
                    ? "neg"
                    : "flat";
              return (
                <button
                  key={i}
                  className={`cal-cell ${cls} ${row ? "has-data" : ""}`}
                  disabled={!row || loading}
                  aria-label={`${dateStr}${row ? `, ${row.trade_count} trades` : ", no trades"}`}
                  aria-pressed={selected === dateStr}
                  onClick={() => row && setSelected(dateStr)}
                >
                  <div className="cal-day">{d}</div>
                  {row && (
                    <>
                      <div className="cal-pnl">${fmt(pnl)}</div>
                      <div className="cal-trades">
                        {row.trade_count} trades
                        {row.open_count > 0 && (
                          <span className="cal-open">
                            {" "}
                            · {row.open_count} open
                          </span>
                        )}
                      </div>
                    </>
                  )}
                </button>
              );
            })}
          </div>
        </section>
        {!selected && (
          <section className="card journal-prompt">
            <h2>Session review</h2>
            <p className="muted">
              Select a trading day to inspect its P&L, positions and activity
              log.
            </p>
          </section>
        )}
        {selected && !detail && (
          <section className="card" role="status">
            {detailError
              ? `Could not load session: ${detailError}`
              : "Loading session…"}
          </section>
        )}

        {selected && detail && (
          <div className="card day-detail">
            <div className="row between center">
              <h2>{selected}</h2>
              <button
                className="btn ghost"
                onClick={() => {
                  setSelected(null);
                  setDetail(null);
                }}
              >
                ✕
              </button>
            </div>
            <div
              className={`big-pnl ${detail.realized_pnl >= 0 ? "pos" : "neg"}`}
            >
              ${detail.realized_pnl.toLocaleString()}
            </div>
            <h4>Trades ({detail.trades.length})</h4>
            {detail.trades.length === 0 ? (
              <div className="muted">No trades.</div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Qty</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>P&L</th>
                    <th>Return</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.trades.map((t) => (
                    <tr key={t.id}>
                      <td>{t.symbol}</td>
                      <td>{t.side}</td>
                      <td>{t.quantity}</td>
                      <td>{t.entry_price ?? "—"}</td>
                      <td>{t.exit_price ?? "—"}</td>
                      <td
                        className={
                          t.pnl == null ? "" : t.pnl >= 0 ? "pos" : "neg"
                        }
                      >
                        {t.pnl != null ? `$${t.pnl.toFixed(2)}` : "—"}
                      </td>
                      <td>
                        {t.return_pct != null
                          ? `${(t.return_pct * 100).toFixed(2)}%`
                          : "—"}
                      </td>
                      <td>
                        <span
                          className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}
                        >
                          {t.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <h4>Options ({(detail.option_trades || []).length})</h4>
            {(detail.option_trades || []).length === 0 ? (
              <div className="muted">No option trades.</div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Underlying</th>
                    <th>Source</th>
                    <th>Contracts</th>
                    <th>P&L</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.option_trades.map((o, i) => (
                    <tr key={i}>
                      <td>{o.underlying}</td>
                      <td className="muted small">
                        {o.source === "triple_calendar"
                          ? "Triple Calendar"
                          : "Builder"}
                      </td>
                      <td className="num">{o.contracts}</td>
                      <td
                        className={
                          o.pnl == null ? "" : o.pnl >= 0 ? "pos" : "neg"
                        }
                      >
                        {o.pnl != null ? `$${o.pnl.toFixed(2)}` : "—"}
                      </td>
                      <td>
                        <span
                          className={`pill tiny ${o.status === "open" ? "live" : "idle"}`}
                        >
                          {o.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <h4>Log ({detail.logs.length})</h4>
            <div className="log-list">
              {detail.logs.map((l) => (
                <div className="log-line" key={l.id}>
                  <span className="muted small">{l.ts?.slice(11, 19)}</span>
                  <span className={`pill tiny ${l.category}`}>
                    {l.category}
                  </span>
                  <span>{l.message}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
