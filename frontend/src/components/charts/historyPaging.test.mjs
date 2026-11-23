// Plain-node test (no runner configured). Run: node frontend/src/components/charts/historyPaging.test.mjs
import { HISTORY_SYMBOLS, cursorMatches, mergePage, offsetAfterAppend, shouldLoad } from "./historyPaging.js";

const eq = (a, b, msg) => {
  if (a !== b) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};
const bar = t => ({ time: t, open: 1, high: 2, low: 0, close: 1, volume: 1 });

eq(HISTORY_SYMBOLS.join(","), "SPY,QQQ,TSLA,NVDA,SPX,NDX", "history symbols");

// mergeKeepsOrder
const base = [3, 4, 5].map(bar);
const older = mergePage(base, [1, 2, 3].map(bar), "before");
eq(older.map(b => b.time).join(","), "1,2,3,4,5", "before page merges ascending, overlap deduped");
const newer = mergePage(older, [5, 6, 7].map(bar), "after");
eq(newer.map(b => b.time).join(","), "1,2,3,4,5,6,7", "after page merges ascending, overlap deduped");
eq(mergePage(null, [2, 1].map(bar), "after").map(b => b.time).join(","), "1,2", "null base and unsorted page");
eq(mergePage(base, [], "before"), base, "empty page returns same bars");

// pagingDedupes
const idle = { hasMoreBefore: true, hasMoreAfter: true, inFlight: { before: false, after: false } };
const near = { length: 100, offset: 450 }; // left edge at index 50
eq(shouldLoad(near, 600, idle, "before"), true, "loads when left edge within 100 bars of the start");
eq(shouldLoad({ length: 100, offset: 50 }, 600, idle, "before"), false, "no load far from the left edge");
eq(shouldLoad(near, 600, { ...idle, inFlight: { before: true, after: false } }, "before"), false, "no second request while one is in flight");
eq(shouldLoad(near, 600, { ...idle, hasMoreBefore: false }, "before"), false, "no load when nothing older exists");
eq(shouldLoad({ length: 100, offset: 20 }, 600, idle, "after"), true, "loads newer when right edge within 100 bars of the end");
eq(shouldLoad({ length: 100, offset: 200 }, 600, idle, "after"), false, "no newer load far from the right edge");
eq(shouldLoad({ length: 100, offset: 20 }, 600, { ...idle, hasMoreAfter: false }, "after"), false, "no load when at newest");
eq(shouldLoad({ length: 100, offset: 20 }, 600, { ...idle, inFlight: { before: false, after: true } }, "after"), false, "after in flight");
eq(shouldLoad({ length: 100, offset: 450 }, 0, idle, "before"), false, "no load with no bars");

// appendShiftsOffset
eq(offsetAfterAppend(20, 600), 620, "append shifts offset from newest");

// staleCursorRejected: a page requested from an old region must not merge into new bars
const region = [10, 11, 12].map(bar);
eq(cursorMatches(region, "after", 12), true, "after cursor equals last bar");
eq(cursorMatches(region, "before", 10), true, "before cursor equals first bar");
eq(cursorMatches(region, "after", 5), false, "after cursor from an old region is rejected");
eq(cursorMatches(region, "before", 12), false, "before cursor from an old region is rejected");
eq(cursorMatches([], "after", 12), false, "no bars rejects");
console.log("historyPaging tests passed");
