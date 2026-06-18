import { useEffect, useState } from "react";
import { api } from "../api.js";

const fmt = (n, d = 2) =>
  n === null || n === undefined
    ? "—"
    : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

const pct = (p) =>
  p === null || p === undefined ? "" : `${p >= 0 ? "+" : ""}${(p * 100).toFixed(2)}%`;

const signClass = (n) => (n === null || n === undefined ? "" : n >= 0 ? "pos" : "neg");

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
          <span className={`ticker-chg ${signClass(ix.change)}`}>
            {ix.change >= 0 ? "▲" : "▼"} {fmt(Math.abs(ix.change ?? 0))}
            {ix.change_pct !== null && ix.change_pct !== undefined && (
              <span className="ticker-chg-pct"> ({pct(ix.change_pct)})</span>
            )}
          </span>
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

// Market-overview hero: live index board shown front-and-centre once the session
// is connected to the market. Prices arrive **streamed** via the WebSocket snapshot
// (`snapshot.market_overview`), updated continuously server-side from a persistent
// DXLink feed. Falls back to polling the REST endpoint only if the socket hasn't
// delivered an overview yet (initial render, or a stream hiccup) — so the board
// always shows something.
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
  const featured = indices.find((i) => i.featured);
  const rest = indices.filter((i) => !i.featured);
  const live = overview?.data_available;

  return (
    <section className="overview card">
      <div className="overview-head">
        <h2>Market Overview</h2>
        <span className={`feed-badge ${live ? "live" : ""}`}>
          <span className="dot" /> {live ? (overview?.streaming ? "Live · streaming" : "Live market data") : "Connecting…"}
        </span>
      </div>

      {overview?.note && <div className="muted small overview-note">{overview.note}</div>}

      {loading && indices.length === 0 ? (
        <div className="muted overview-loading">Connecting to market…</div>
      ) : (
        <div className="ticker-board">
          {featured && <Ticker ix={featured} featured />}
          <div className="ticker-row">
            {rest.map((ix) => (
              <Ticker key={ix.symbol} ix={ix} />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
