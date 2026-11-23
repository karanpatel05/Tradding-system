import { useEffect, useRef, useState } from "react";
import { api } from "../../api.js";
import { aggregate, normalizeBars, weekly } from "./chartMath.js";
import { getBars } from "./chartData.js";
import { HISTORY_SYMBOLS, cursorMatches, historyUsable, mergePage } from "./historyPaging.js";

// Chart intervals mapped to the candle interval the feed serves and how many of
// those make one chart bar. The feed has 1m/5m/15m/1d; 30m and 1h are built from 15m.
export const FEED_INTERVAL = {
  "1m": ["1m", 1], "5m": ["5m", 1], "15m": ["15m", 1],
  "30m": ["15m", 2], "1h": ["15m", 4], "4h": ["15m", 16], "1D": ["1d", 1], "1W": ["1d", 1],
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
    if (!symbol) return;
    let alive = true;
    const [feed, factor] = FEED_INTERVAL[interval] || FEED_INTERVAL["5m"];
    lastLive.current = null;
    setState({ bars: null, live: false, stale: false, note: null });
    const run = () => loadFeed(symbol, feed).then(({ bars, note }) => {
      if (!alive) return;
      if (bars.length) {
        lastLive.current = interval === "1W" ? weekly(bars) : aggregate(bars, factor);
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

const PAGE = 600;
const NO_HISTORY = { bars: null, live: false, stale: false, note: null, earliest: null,
  hasMoreBefore: false, hasMoreAfter: false, appended: 0, focusTime: null };

// Paged history for the six stored symbols. Loads the newest page, then older/newer pages on
// demand (`loadMore`), jumps to a date (`goToDate`) and back (`backToLive`). While at the
// newest bars it re-polls so stored minutes from the background sync appear. `appended` is
// the count added on the right by the last loadMore("after"); `focusTime` is the ms of the
// last goToDate. A response that lands after symbol/interval/view changed is discarded.
// `fallback` is set while the store has nothing recent to show (empty, stale or unreachable): the
// pane then uses the live feed loader, and this hook keeps retrying the newest page until it is usable.
export function useHistoryBars(symbol, interval) {
  const [state, setState] = useState(NO_HISTORY);
  const cur = useRef(state);
  cur.current = state;
  const gen = useRef(0);
  const inFlight = useRef({ before: false, after: false });
  const atLive = useRef(true);
  const replacing = useRef(false);

  const fail = (e, g) => {
    if (g !== gen.current) return;
    const note = e.status === 401 ? "Market data needs the app access token (Connections)."
      : e.message || "History request failed.";
    setState(s => s.bars && s.live ? { ...s, stale: true, note, appended: 0 }
      : { ...NO_HISTORY, fallback: true, note });
  };
  // Replace the loaded bars with a fresh page (newest, or around a date).
  const load = (params, focusTime) => {
    const g = ++gen.current;
    inFlight.current = { before: false, after: false };
    replacing.current = true;
    return api.marketHistory({ symbol, interval, limit: PAGE, ...params }).then(r => {
      if (g !== gen.current) return;
      if (!params.around && !historyUsable(r, Date.now())) {
        setState({ ...NO_HISTORY, fallback: true, note: r.note || "Local history is not current yet." });
        return;
      }
      if (!(r.bars || []).length) throw new Error(r.note || "No history bars returned.");
      atLive.current = !params.around;
      setState({ bars: normalizeBars(r.bars || []), live: true, stale: false, note: r.note || null,
        earliest: r.earliest ?? null, hasMoreBefore: !!r.has_more_before, hasMoreAfter: !!r.has_more_after,
        appended: 0, focusTime });
    }).catch(e => fail(e, g)).finally(() => { if (g === gen.current) replacing.current = false; });
  };

  useEffect(() => {
    if (!symbol) return;
    atLive.current = true;
    replacing.current = false;
    setState(NO_HISTORY);
    load({}, null);
    const feed = (FEED_INTERVAL[interval] || FEED_INTERVAL["5m"])[0];
    const id = setInterval(() => {
      const s = cur.current;
      if (replacing.current || !atLive.current) return;
      if (!s.live) { load({}, null); return; } // nothing real loaded yet: retry the first page
      if (s.hasMoreAfter || inFlight.current.after || inFlight.current.before) return;
      const g = gen.current;
      api.marketHistory({ symbol, interval, limit: PAGE }).then(r => {
        if (g !== gen.current) return;
        const page = normalizeBars(r.bars || []);
        setState(p => !page.length || !p.bars?.length ? { ...p, stale: false, note: null }
          : { ...p, bars: page[0].time > p.bars.at(-1).time ? page : mergePage(p.bars, page, "after"),
              stale: false, note: r.note || null, appended: 0 });
      }).catch(e => fail(e, g));
    }, REFRESH_MS[feed]);
    return () => { gen.current++; clearInterval(id); };
  }, [symbol, interval]);

  const loadMore = side => {
    const s = cur.current;
    if (replacing.current || !s.live || !s.bars?.length || inFlight.current[side] || !(side === "before" ? s.hasMoreBefore : s.hasMoreAfter)) return;
    const g = gen.current;
    inFlight.current[side] = true;
    const cursor = side === "before" ? s.bars[0].time : s.bars.at(-1).time;
    api.marketHistory({ symbol, interval, limit: PAGE, [side]: cursor }).then(r => {
      if (g !== gen.current) return;
      const page = normalizeBars(r.bars || []);
      setState(p => {
        if (!cursorMatches(p.bars, side, cursor)) return p;
        const bars = mergePage(p.bars, page, side);
        return side === "before"
          ? { ...p, bars, hasMoreBefore: !!r.has_more_before, earliest: r.earliest ?? p.earliest, stale: false, note: r.note || null, appended: 0 }
          : { ...p, bars, hasMoreAfter: !!r.has_more_after, stale: false, note: r.note || null, appended: bars.length - p.bars.length };
      });
    }).catch(e => fail(e, g)).finally(() => { if (g === gen.current) inFlight.current[side] = false; });
  };
  const goToDate = iso => {
    const [y, m, d] = iso.split("-").map(Number);
    return load({ around: iso }, Date.UTC(y, m - 1, d));
  };
  const backToLive = () => load({}, null);
  return { ...state, loadMore, goToDate, backToLive };
}

// Six stored symbols page through the history API; others keep the live feed polling. Until the store has
// recent bars (first backfill, sync off, Alpaca keys rejected) the six use the live feed loader too, without
// scroll-back or go-to-date (the feed state has no paging fields).
export function usePaneBars(symbol, interval, background = false) {
  const history = HISTORY_SYMBOLS.includes(symbol);
  const paged = useHistoryBars(history ? symbol : null, interval);
  const useFeed = !history || !!paged.fallback;
  const feed = useBars(useFeed ? symbol : null, interval, background);
  return useFeed ? feed : paged;
}

// Watchlist quotes. Keeps the last live quotes (marked stale) when a refresh fails;
// falls back to the labelled sample sessions only when nothing live has loaded.
export function useQuotes(symbols) {
  const list = symbols.join(",");
  const [state, setState] = useState({ quotes: {}, live: false, stale: false, note: null });
  useEffect(() => {
    let alive = true, lastLive = null;
    const sample = () => Object.fromEntries(list.split(",").map(s => {
      const days = getBars(s, "1D");
      const price = days.at(-1).close, close = days.at(-2).close;
      return [s, { price, close, change_pct: (price / close - 1) * 100 }];
    }));
    const fail = note => setState(lastLive
      ? { quotes: lastLive, live: true, stale: true, note }
      : { quotes: sample(), live: false, stale: false, note });
    const run = () => api.marketQuotes(list.split(","))
      .then(r => {
        if (!alive) return;
        if (r.data_available) { lastLive = r.quotes; setState({ quotes: r.quotes, live: true, stale: false, note: null }); }
        else fail(r.note);
      })
      .catch(e => alive && fail(e.message));
    run();
    const id = setInterval(run, 15000);
    return () => { alive = false; clearInterval(id); };
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
