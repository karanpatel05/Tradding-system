// Plain-node test (no runner configured). Run: node frontend/src/components/charts/chartMath.test.mjs
import { aggregate, crossovers, ema, normalizeBars, rsi, sessionOf, sessionStats } from "./chartMath.js";
import { studyValues } from "./chartData.js";

const eq = (a, b, msg) => {
  const ok = typeof b === "number" ? Math.abs(a - b) < 1e-6 : a === b;
  if (!ok) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};

// 14:30 UTC on 20 Nov 2026 is 09:30 in New York; 03:00 UTC is still the 19th there.
eq(sessionOf(Date.UTC(2026, 10, 20, 14, 30)), "2026-11-20", "session is the New York date");
eq(sessionOf(Date.UTC(2026, 10, 20, 3, 0)), "2026-11-19", "late UTC night belongs to prior ET day");

const raw = [0, 1, 2, 3, 4].map(i => ({
  time: Date.UTC(2026, 10, 20, 14, 30 + i * 15), open: 100 + i, high: 102 + i, low: 99 + i,
  close: 101 + i, volume: 10,
}));
const bars = normalizeBars(raw);
eq(bars[0].session, "2026-11-20", "normalized bar has session");
eq(bars[0].value, (102 + 99 + 101) / 3 * 10, "value is typical price times volume");

const hourly = aggregate(bars, 4);
eq(hourly.length, 2, "five 15m bars make one full hour and one partial");
eq(hourly[0].open, 100, "aggregate keeps first open");
eq(hourly[0].close, 104, "aggregate keeps last close");
eq(hourly[0].high, 105, "aggregate takes max high");
eq(hourly[0].low, 99, "aggregate takes min low");
eq(hourly[0].volume, 40, "aggregate sums volume");
eq(bars[0].close, 101, "aggregate does not mutate input");

const nextDay = normalizeBars([{ ...raw[0], time: Date.UTC(2026, 10, 23, 14, 30) }]);
eq(aggregate([...bars.slice(0, 2), ...nextDay], 4).length, 2, "aggregate never merges across sessions");

const e = ema([10, 20], 3);
eq(e[0], 10, "ema seeds with first value");
eq(e[1], 15, "ema k = 2/(n+1)");

eq(rsi([1, 2, 3], 14)[2], null, "rsi needs more than period values");
const rising = Array.from({ length: 20 }, (_, i) => i);
eq(rsi(rising, 14)[14], 100, "only gains gives rsi 100");
eq(rsi(rising, 14)[13], null, "values before the period are null");
const zigzag = Array.from({ length: 30 }, (_, i) => (i % 2 ? 1 : 0));
const mid = rsi(zigzag, 14)[29];
eq(mid > 45 && mid < 55, true, "equal gains and losses gives rsi near 50");

const cross = crossovers([1, 2, 3, 2, 1], [2, 2, 2, 2, 2]);
eq(cross.length, 2, "one cross up and one cross down");
eq(cross[0].index, 2, "cross up happens when fast first exceeds slow");
eq(cross[0].direction, "up", "first cross is up");
eq(cross[1].index, 4, "cross down when fast drops below");
eq(cross[1].direction, "down", "second cross is down");

const twoDays = normalizeBars([
  { time: Date.UTC(2026, 10, 19, 20, 55), open: 99, high: 100, low: 98, close: 100, volume: 5 },
  ...raw,
]);
const stats = sessionStats(twoDays);
eq(stats.prevClose, 100, "previous close from prior session");
eq(stats.last, 105, "last close");
eq(stats.changePct, 5, "change percent from previous close");
eq(stats.open, 100, "session open");
eq(stats.high, 106, "session high");
eq(stats.low, 99, "session low");
eq(stats.volume, 50, "session volume excludes prior day");
eq(sessionStats([]), null, "no bars gives no stats");

// Live sessions can open with zero-volume bars; VWAP must stay a finite number.
const quiet = normalizeBars([{ ...raw[0], volume: 0 }, raw[1]]);
const vwap = studyValues(quiet, { type: "VWAP" });
eq(Number.isFinite(vwap[0]), true, "zero-volume first bar gives finite VWAP");
eq(vwap[0], quiet[0].close, "VWAP uses the close until volume arrives");
eq(vwap[1], quiet[1].value / quiet[1].volume, "VWAP accumulates once volume arrives");

console.log("chartMath: all tests passed");
