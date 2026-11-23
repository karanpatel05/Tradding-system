import { sessionOf } from "./chartMath.js";

// Fold one live trade into the loaded bars. Pure: returns `bars` itself when nothing changes.
// intervalMs 0/undefined (1D, 1W) means only the last bar is ever updated, never a new one.
// A tick older than the last bar's start (e.g. last night's after-hours print) is ignored.
export function foldLivePrice(bars, price, tsMs, intervalMs) {
  const last = bars?.at(-1);
  if (!last || !Number.isFinite(price) || !Number.isFinite(tsMs) || tsMs < last.time) return bars;
  if (!intervalMs || tsMs < last.time + intervalMs) {
    const next = { ...last, close: price, high: Math.max(last.high, price), low: Math.min(last.low, price) };
    return [...bars.slice(0, -1), next];
  }
  const time = Math.floor(tsMs / intervalMs) * intervalMs;
  return [...bars, { time, session: sessionOf(time), open: price, high: price, low: price, close: price, volume: 0, value: 0 }];
}

const FOLD_MS = { "1m": 60e3, "5m": 300e3, "15m": 900e3, "30m": 1800e3, "1h": 3600e3 };
const FOUR_HOURS = 4 * 3600e3;

// foldLivePrice for a chart interval. 1D/1W only update the last bar. 4h buckets sit on ET clock
// boundaries (not UTC multiples), so a 4h pane never opens a bar itself: it updates the last bar
// while the tick is inside it and leaves the next bar to the history refresh.
export function foldForInterval(bars, price, tsMs, interval) {
  if (interval === "4h") {
    const last = bars?.at(-1);
    return last && tsMs >= last.time + FOUR_HOURS ? bars : foldLivePrice(bars, price, tsMs, 0);
  }
  return foldLivePrice(bars, price, tsMs, FOLD_MS[interval] || 0);
}
