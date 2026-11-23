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

// ET Monday (yyyy-mm-dd) of the week containing a time.
function weekOf(time) {
  const [y, m, d] = sessionOf(time).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d - (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7)).toISOString().slice(0, 10);
}

// foldLivePrice for a chart interval. 1D/1W only update the last bar, and only when the tick is in the same
// ET session / Monday-week; a later day's tick leaves the bars alone until the history refresh brings the new bar.
// 4h buckets sit on ET clock boundaries (not UTC multiples), so a 4h pane never opens a bar itself either: it
// updates the last bar while the tick is inside it.
export function foldForInterval(bars, price, tsMs, interval) {
  const last = bars?.at(-1);
  if (last && interval === "4h" && tsMs >= last.time + FOUR_HOURS) return bars;
  if (last && interval === "1D" && sessionOf(tsMs) !== sessionOf(last.time)) return bars;
  if (last && interval === "1W" && weekOf(tsMs) !== weekOf(last.time)) return bars;
  return foldLivePrice(bars, price, tsMs, FOLD_MS[interval] || 0);
}

// New stored bars replace the old ones; bars built from live ticks that are newer than the newest stored bar
// carry over. A stored bar at the same time as a live bar wins.
export function rebaseLive(stored, folded) {
  const last = stored?.at(-1);
  if (!last || !folded) return stored;
  const keep = folded.filter(b => b.time > last.time);
  return keep.length ? [...stored, ...keep] : stored;
}
