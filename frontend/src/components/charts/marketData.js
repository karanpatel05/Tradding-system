import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api.js";
import { aggregate, carryNewer, foldPrice, normalizeBars } from "./chartMath.js";
import { getBars } from "./chartData.js";

// Chart intervals mapped to the candle interval the feed serves and how many of
// those make one chart bar. The feed has 1m/5m/15m/1d; 30m and 1h are built from 15m.
export const FEED_INTERVAL = {
  "1m": ["1m", 1], "5m": ["5m", 1], "15m": ["15m", 1],
  "30m": ["15m", 2], "1h": ["15m", 4], "1D": ["1d", 1],
};
// Candle history shares a one-at-a-time collector with the live strategies' bar
// fetches, so refresh sparingly: the selected pane at these rates, others every 5 min.
const REFRESH_MS = { "1m": 60000, "5m": 60000, "15m": 120000, "1d": 600000 };
const BACKGROUND_MS = 300000;

// One fetch per (symbol, feed interval) shared by every pane that shows it.
const cache = new Map();
function loadFeed(symbol, feed) {
  const key = symbol + "|" + feed;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < REFRESH_MS[feed] - 1000) return hit.promise;
  const promise = api.marketBars(symbol, feed)
    .then(r => ({ bars: normalizeBars(r.bars || []), note: r.note }))
    .catch(e => ({ bars: [], note: e.status === 401
      ? "Market data needs the app access token (Connections)."
      : e.message || "Market data request failed." }));
  cache.set(key, { at: Date.now(), promise });
  return promise;
}

// Bars for one pane. Live feed bars when available. If a refresh fails after live
// bars arrived, the last live bars stay up marked stale; the fixed sample session is
// used only when nothing live has loaded. `bars` is null until the first answer.
export function useBars(symbol, interval, background = false) {
  const [state, setState] = useState({ bars: null, live: false, stale: false, note: null });
  const lastLive = useRef(null);
  useEffect(() => {
    let alive = true;
    const [feed, factor] = FEED_INTERVAL[interval] || FEED_INTERVAL["5m"];
    lastLive.current = null;
    setState({ bars: null, live: false, stale: false, note: null });
    const run = () => loadFeed(symbol, feed).then(({ bars, note }) => {
      if (!alive) return;
      if (bars.length) {
        lastLive.current = aggregate(bars, factor);
        setState({ bars: lastLive.current, live: true, stale: false, note: null });
      } else if (lastLive.current) {
        setState({ bars: lastLive.current, live: true, stale: true, note });
      } else {
        setState({ bars: getBars(symbol, interval), live: false, stale: false, note });
      }
    });
    run();
    const id = setInterval(run, background ? Math.max(BACKGROUND_MS, REFRESH_MS[feed]) : REFRESH_MS[feed]);
    return () => { alive = false; clearInterval(id); };
  }, [symbol, interval, background]);
  return state;
}

// Watchlist and chart-pane quotes. Keeps the last live quotes (marked stale) when a refresh fails;
// falls back to the labelled sample sessions only when nothing live has loaded. Polls every 5 s in
// the regular session (the chart folds these prices into its newest candle), 15 s otherwise.
export function useQuotes(symbols) {
  const list = symbols.join(",");
  const [state, setState] = useState({ quotes: {}, live: false, stale: false, note: null, session: null });
  useEffect(() => {
    let alive = true, lastLive = null, timer = null, session = null;
    const sample = () => Object.fromEntries(list.split(",").map(s => {
      const days = getBars(s, "1D");
      const price = days.at(-1).close, close = days.at(-2).close;
      return [s, { price, close, change_pct: (price / close - 1) * 100 }];
    }));
    const fail = note => setState(lastLive
      ? { quotes: lastLive, live: true, stale: true, note, session: null }
      : { quotes: sample(), live: false, stale: false, note, session: null });
    const run = () => api.marketQuotes(list.split(","))
      .then(r => {
        if (!alive) return;
        session = r.session || null;
        if (r.data_available) { lastLive = r.quotes; setState({ quotes: r.quotes, live: true, stale: false, note: null, session }); }
        else fail(r.note);
      })
      .catch(e => alive && fail(e.message))
      .finally(() => { if (alive) timer = setTimeout(run, session === "regular" ? 5000 : 15000); });
    run();
    return () => { alive = false; clearTimeout(timer); };
  }, [list]);
  return state;
}

// App-owned broker positions (Alpaca paper). Never substituted with sample data.
export function usePositions() {
  const [state, setState] = useState({ positions: null, note: null });
  useEffect(() => {
    let alive = true;
    const run = () => api.positions()
      .then(r => alive && setState({ positions: r.positions || [], note: r.note }))
      .catch(e => alive && setState({ positions: null, note: e.message }));
    run();
    const id = setInterval(run, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return state;
}

// The pane's candles with live quotes folded into the newest one. `quote` is null outside the
// regular session or when quotes are stale, which leaves the candles exactly as fetched. Folded
// candles newer than a fresh candle fetch carry over until the server has built them.
export function useLiveCandle(bars, quote, interval) {
  const ref = useRef({ base: null, folded: null, interval: null });
  return useMemo(() => {
    const L = ref.current;
    if (!bars) { ref.current = { base: null, folded: null, interval }; return bars; }
    let cur = L.interval !== interval ? bars : L.base === bars ? L.folded : carryNewer(bars, L.folded);
    if (quote && Number.isFinite(quote.price)) cur = foldPrice(cur, quote.price, Date.now(), interval);
    ref.current = { base: bars, folded: cur, interval };
    return cur;
  }, [bars, quote, interval]);
}
