import { useEffect, useState } from "react";
import { api } from "../api.js";
import { subscribeMarketTicks } from "../hooks/marketTicks.js";

const tradeTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hourCycle: "h23",
  hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3,
});
const tradePrice = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const tradeSize = new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 });

const fmt = (n, d = 2) =>
  n === null || n === undefined
    ? "—"
    : Number(n).toLocaleString(undefined, {
        minimumFractionDigits: d,
        maximumFractionDigits: d,
      });

const pct = (p) =>
  p === null || p === undefined
    ? ""
    : `${p >= 0 ? "+" : ""}${(p * 100).toFixed(2)}%`;

const signClass = (n) =>
  n === null || n === undefined ? "" : n >= 0 ? "pos" : "neg";

// A single index ticker. VIX (kind: "volatility") is a level, not a dollar
// price, so it gets no "$" and its rise/fall is shown neutrally-coloured by sign.
function Ticker({ ix, featured }) {
  const hasData = ix.last !== null && ix.last !== undefined;
  return (
    <div className={`ticker ${featured ? "featured" : ""} ${ix.kind}`}>
      <div className="ticker-head">
        <span className="ticker-name">{ix.name}</span>
        <span className="ticker-sym">{ix.symbol}</span>
      </div>
      <div className="ticker-last">{fmt(ix.last)}</div>
      {hasData ? (
        <div className="ticker-changes">
          {ix.change != null && <span className={`ticker-chg ${signClass(ix.change)}`}>
            {ix.change >= 0 ? "▲" : "▼"} {fmt(Math.abs(ix.change))}
            {ix.change_pct !== null && ix.change_pct !== undefined && (
              <span className="ticker-chg-pct"> ({pct(ix.change_pct)})</span>
            )}
          </span>}
          {ix.week_change_pct !== null && ix.week_change_pct !== undefined && (
            <span className={`ticker-week ${signClass(ix.week_change_pct)}`}>
              1W {pct(ix.week_change_pct)}
            </span>
          )}
        </div>
      ) : (
        <div className="ticker-changes muted small">no data</div>
      )}
    </div>
  );
}

// The overview supplies reference prices and weekly returns. While the tape
// is connected, newer last sales take precedence over its periodic snapshots.
export default function MarketOverview({ snapshot }) {
  const streamed = snapshot?.market_overview;
  const [polled, setPolled] = useState(null);
  const [loading, setLoading] = useState(true);

  // REST fallback: only runs while the stream hasn't provided an overview.
  useEffect(() => {
    if (streamed) {
      setLoading(false);
      return;
    }
    let alive = true;
    const load = () =>
      api
        .marketOverview()
        .then((d) => {
          if (alive) {
            setPolled(d);
            setLoading(false);
          }
        })
        .catch(() => alive && setLoading(false));
    load();
    const id = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [streamed]);

  const overview = streamed || polled;
  const indices = overview?.indices || [];
  const symbols = indices.map(ix => ix.symbol).join(",");
  const [latest, setLatest] = useState({});
  const [recent, setRecent] = useState([]);
  const [tradeStatus, setTradeStatus] = useState("offline");
  useEffect(() => {
    if (!symbols) return;
    let sequence = 0;
    setLatest({});
    setRecent([]);
    return subscribeMarketTicks(symbols.split(","), ticks => {
      setTradeStatus("streaming");
      setLatest(previous => {
        const next = { ...previous };
        for (const tick of ticks) {
          if (tick.updatesPrice === false) continue;
          if (!next[tick.symbol] || tick.time >= next[tick.symbol].time) next[tick.symbol] = tick;
        }
        return next;
      });
      const rows = ticks.slice(-10).map(tick => ({ ...tick, id: ++sequence })).reverse();
      setRecent(previous => [...rows, ...previous].slice(0, 10));
    }, ready => {
      setTradeStatus(ready ? "waiting" : "offline");
      if (!ready) setLatest({});
    });
  }, [symbols]);
  const hasTicks = tradeStatus === "streaming";
  const regularSession = overview?.status?.session === "regular";
  const feedLabel = hasTicks && regularSession ? "Live trades"
    : tradeStatus === "waiting" && regularSession ? "Waiting for trades"
    : overview?.data_available || hasTicks ? "Last prices"
    : loading ? "Connecting…" : "Unavailable";

  return (
    <section className="overview card">
      <div className="overview-head">
        <h2>Market Overview</h2>
        <span className={`feed-badge ${hasTicks && regularSession ? "live" : ""}`}>
          <span className="dot" />{" "}
          {feedLabel}
        </span>
      </div>

      {overview?.note && !hasTicks && (
        <div className="muted small overview-note">{overview.note}</div>
      )}

      {loading && indices.length === 0 ? (
        <div className="muted overview-loading">Connecting to market…</div>
      ) : (
        <div className="ticker-board">
          <div className="ticker-row">
            {indices.map(ix => {
              const print = hasTicks && latest[ix.symbol];
              const change = print && ix.prev_close > 0 ? print.price - ix.prev_close : null;
              const row = print ? { ...ix, last: print.price, change,
                change_pct: change === null ? null : change / ix.prev_close } : ix;
              return <Ticker key={ix.symbol} ix={row} />;
            })}
            {indices.length === 0 && (
              <p className="muted small">
                Market quotes are unavailable. Check the market data connection.
              </p>
            )}
          </div>
          {indices.length > 0 && <details className="overview-trades">
            <summary>Recent trades</summary>
            {recent.length ? <table>
              <thead><tr><th>Time (ET)</th><th>Symbol</th><th>Price</th><th>Shares</th></tr></thead>
              <tbody>{recent.map(tick => <tr key={tick.id}>
                <td>{tradeTime.format(tick.time)}</td><td>{tick.symbol}</td>
                <td>{tradePrice.format(tick.price)}</td><td>{tradeSize.format(tick.size)}</td>
              </tr>)}</tbody>
            </table> : <p className="muted small">Regular-session trades appear here as they arrive.</p>}
          </details>}
        </div>
      )}
    </section>
  );
}
