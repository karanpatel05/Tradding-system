// Pure helpers for paging price history into the chart. No React, no network.

// Symbols the backend history store serves (/api/market/history).
export const HISTORY_SYMBOLS = ["SPY", "QQQ", "TSLA", "NVDA", "SPX", "NDX"];

// Merge a fetched page into the loaded bars: unique by time, ascending. A bar in the
// page replaces a loaded bar with the same time (the newest page refreshes the forming bar).
export function mergePage(bars, page, side) {
  if (!page || !page.length) return bars || [];
  const byTime = new Map();
  const [first, second] = side === "before" ? [page, bars || []] : [bars || [], page];
  for (const b of first) byTime.set(b.time, b);
  for (const b of second) byTime.set(b.time, b);
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

// Should a page be requested for `side`? view = {length, offset} with offset counted from
// the newest bar, as in PriceChart. One request per side at a time; nothing when the store
// has no more in that direction.
export function shouldLoad(view, total, loaded, side, threshold = 100) {
  if (!total || loaded.inFlight[side]) return false;
  if (side === "before") return loaded.hasMoreBefore && total - view.offset - view.length < threshold;
  return loaded.hasMoreAfter && view.offset < threshold;
}

// Bars appended on the right keep the same view only if the offset grows by that count;
// a prepend needs no change because the offset is counted from the newest bar.
export function offsetAfterAppend(offset, appendedCount) {
  return offset + appendedCount;
}
