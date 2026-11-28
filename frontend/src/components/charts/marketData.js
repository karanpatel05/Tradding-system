import { useEffect, useRef, useState } from "react";
import { api } from "../../api.js";
import { subscribeMarketTicks } from "../../hooks/marketTicks.js";
import { aggregate, applyTradeTicks, normalizeBars, reconcileHistory } from "./chartMath.js";
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
function loadFeed(symbol, feed, recovery = false) {
  const key = symbol + "|" + feed;
  const hit = cache.get(key);
  if (hit && (recovery ? hit.recovery && hit.pending : hit.pending || Date.now() - hit.at < REFRESH_MS[feed] - 1000)) {
    return hit.promise;
  }
  const entry = { at: Date.now(), recovery, pending: true };
  const fetchBars = () => api.marketBars(symbol, feed, recovery)
    .then(r => ({ bars: normalizeBars(r.bars || [], feed), note: r.note, archived: r.source === "archive" }))
    .catch(e => ({ bars: [], note: e.status === 401
      ? "Market data needs the app access token (Connections)."
      : e.message || "Market data request failed." }));
  // A snapshot already in flight at the gap may predate the missing trades.
  entry.promise = (hit?.pending ? hit.promise.then(fetchBars) : fetchBars())
    .finally(() => { entry.pending = false; });
  cache.set(key, entry);
  return entry.promise;
}

// Incoming trades can start a real candle even while historical bars are loading.
// Sample sessions are never used as the base for a real trade.
const emptyBars = { bars: null, live: false, streaming: false, stale: false, note: null };
export function useBars(symbol, interval, background = false) {
  const [result, setResult] = useState(null);
  const backgroundRef = useRef(background);
  backgroundRef.current = background;
  useEffect(() => {
    const [feed, factor] = FEED_INTERVAL[interval] || FEED_INTERVAL["5m"];
    const pendingVolume = new Map();
    let real = [], alive = true, received = false, version = 0, timer;
    let archiveBase = false;
    let wasOnline = false, gap = false, requestId = 0;
    const setState = state => setResult({ symbol, interval, state });

    const unsubscribe = subscribeMarketTicks([symbol], ticks => {
      const next = applyTradeTicks(real, ticks, interval, pendingVolume);
      if (next === real) return;
      real = next;
      // A late or volume-only print can adjust history without establishing a
      // live price for the newest candle. Keep its saved label until that price arrives.
      archiveBase = archiveBase && real.at(-1)?.lastTradeTime == null;
      received = !archiveBase;
      version++;
      setState({ bars: real, live: true, streaming: received, archived: archiveBase, stale: archiveBase, note: null });
    }, online => {
      if (online && gap) {
        gap = false;
        clearTimeout(timer);
        refresh(true);
      } else if (!online && wasOnline) {
        gap = true;
        received = false;
        if (real.length) setState({ bars: real, live: true, streaming: false, archived: archiveBase, stale: true, note: "Trade stream disconnected." });
      }
      wasOnline = online;
    });

    async function refresh(recovery = false) {
      const id = ++requestId;
      const before = version;
      const { bars, note, archived } = await loadFeed(symbol, feed, recovery);
      if (!alive || id !== requestId) return;
      if (bars.length && !archived) {
        real = reconcileHistory(aggregate(bars, factor), real, interval, received || before !== version, pendingVolume);
        archiveBase = false;
      } else if (archived && !received && bars.length && (!real.length || bars.at(-1).time >= real.at(-1).time)) {
        real = bars;
        archiveBase = true;
      }
      if (real.length) {
        setState({ bars: real, live: true, streaming: received, archived: archiveBase,
          stale: (!bars.length || archived) && !received, note });
      } else {
        setState({ bars: getBars(symbol, interval), live: false, streaming: false, stale: false, note });
      }
      const delay = backgroundRef.current ? Math.max(BACKGROUND_MS, REFRESH_MS[feed]) : REFRESH_MS[feed];
      timer = setTimeout(refresh, delay);
    }
    refresh();
    return () => { alive = false; clearTimeout(timer); unsubscribe(); };
  }, [symbol, interval]);
  return result?.symbol === symbol && result.interval === interval ? result.state : emptyBars;
}

function withTrade(quote, price) {
  const close = quote?.close;
  return { ...quote, price, change_pct: close > 0 ? (price / close - 1) * 100 : null };
}

// REST supplies the previous close and a fallback for quiet or disconnected
// feeds. The watchlist paints once per batch; alert checks receive every print.
const emptyQuotes = { quotes: {}, live: false, stale: false, note: null };
export function useQuotes(symbols, onUpdate) {
  const list = symbols.join(",");
  const [state, setState] = useState(emptyQuotes);
  useEffect(() => {
    const symbols = list.split(",").filter(Boolean);
    let alive = true, timer, version = 0, generation = 0, online = false, received = false;
    let quotes = {};
    const latest = new Map();
    const publish = (next, ticks) => {
      setState(next);
      onUpdate?.(next, ticks);
    };
    publish(emptyQuotes);
    const unsubscribe = subscribeMarketTicks(symbols, ticks => {
      const next = { ...quotes };
      const priceUpdates = [];
      for (const tick of ticks) {
        if (tick.updatesPrice === false) continue;
        if (tick.time < (latest.get(tick.symbol)?.time ?? 0)) continue;
        latest.set(tick.symbol, { ...tick, version: ++version, generation });
        next[tick.symbol] = withTrade(next[tick.symbol], tick.price);
        priceUpdates.push(tick);
      }
      if (!priceUpdates.length) return;
      received = online;
      quotes = next;
      publish({ quotes, live: true, stale: false, note: null }, priceUpdates);
    }, connected => {
      online = connected;
      if (!connected) generation++;
      if (!connected && received) {
        received = false;
        publish({ quotes, live: true, stale: true, note: "Trade stream disconnected." });
      }
    });

    const sample = () => Object.fromEntries(symbols.map(s => {
      const days = getBars(s, "1D");
      return [s, withTrade({ close: days.at(-2).close }, days.at(-1).close)];
    }));
    async function refresh() {
      const before = version;
      let note;
      const requests = [];
      for (let i = 0; i < symbols.length; i += 20) {
        requests.push(api.marketQuotes(symbols.slice(i, i + 20)));
      }
      const responses = await Promise.allSettled(requests);
      if (!alive) return;
      let available = false;
      const next = { ...quotes };
      for (const response of responses) {
        if (response.status === "rejected") { note = response.reason.message; continue; }
        const data = response.value;
        note ||= data.note;
        for (const [symbol, quote] of Object.entries(data.quotes || {})) {
          const tick = latest.get(symbol);
          if (tick && ((online && tick.generation === generation) || tick.version > before)) {
            next[symbol] = withTrade({ ...quote, close: quote.close ?? next[symbol]?.close }, tick.price);
          } else if (quote.price != null) {
            next[symbol] = quote;
          }
          available ||= quote.price != null;
        }
      }
      quotes = next;
      const real = Object.keys(quotes).length > 0;
      publish({ quotes: real ? quotes : sample(), live: real,
        stale: real && !received && !available, note: available || received ? null : note });
      timer = setTimeout(refresh, 15000);
    }
    refresh();
    return () => { alive = false; clearTimeout(timer); unsubscribe(); };
  }, [list, onUpdate]);
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
