// Plain-node test (no runner configured). Run: node frontend/src/components/charts/liveCandle.test.mjs
import { foldForInterval, foldLivePrice } from "./liveCandle.js";

const eq = (a, b, msg) => {
  if (a !== b) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};
const M = 60e3, T0 = Date.UTC(2026, 10, 20, 15, 0); // 10:00 ET
const bar = (t, o, h, l, c) => ({ time: t, session: "2026-11-20", open: o, high: h, low: l, close: c, volume: 5, value: 0 });
const bars = [bar(T0 - 5 * M, 9, 10, 8, 9), bar(T0, 10, 11, 9, 10)];

// updatesLastBar: tick inside the last bar moves close and widens high/low, keeps open/volume
let r = foldLivePrice(bars, 12, T0 + 2 * M, 5 * M);
eq(r.length, 2, "updates keep length");
eq(r[1].close, 12, "close follows tick"); eq(r[1].high, 12, "high widens"); eq(r[1].low, 9, "low kept"); eq(r[1].open, 10, "open kept"); eq(r[1].volume, 5, "volume kept");
eq(bars[1].close, 10, "input not mutated");
r = foldLivePrice(bars, 8.5, T0 + 3 * M, 5 * M);
eq(r[1].low, 8.5, "low widens"); eq(r[1].high, 11, "high kept");

// opensNewBarAtBoundary
r = foldLivePrice(bars, 10.5, T0 + 5 * M, 5 * M);
eq(r.length, 3, "new bar appended");
eq(r[2].time, T0 + 5 * M, "new bar start is bucket aligned");
eq(r[2].open, 10.5, "new open"); eq(r[2].high, 10.5, "new high"); eq(r[2].low, 10.5, "new low"); eq(r[2].close, 10.5, "new close"); eq(r[2].volume, 0, "new volume");
eq(r[2].session, "2026-11-20", "new bar has a session");
r = foldLivePrice(bars, 10.5, T0 + 12 * M + 7, 5 * M);
eq(r[2].time, T0 + 10 * M, "gap tick floors to its own bucket");

// ignoresOldTicks: before the last bar's start (last night's print) changes nothing
eq(foldLivePrice(bars, 99, T0 - 1, 5 * M), bars, "old tick returns same array");
eq(foldLivePrice(bars, 99, T0 - 3600e3, 0), bars, "old tick ignored on daily too");

// dailyNeverAppends: intervalMs 0 means update last bar only, even days later
const day = [bar(T0 - 24 * 3600e3, 1, 2, 0.5, 1.5), bar(T0, 10, 11, 9, 10)];
r = foldLivePrice(day, 12, T0 + 3 * 24 * 3600e3, 0);
eq(r.length, 2, "daily keeps length"); eq(r[1].close, 12, "daily close updated"); eq(r[1].high, 12, "daily high updated");

// guards
eq(foldLivePrice([], 1, T0, 5 * M).length, 0, "empty bars untouched");
eq(foldLivePrice(bars, NaN, T0, 5 * M), bars, "bad price ignored");
// foldForInterval: intraday opens bars, 1D/1W/4h only ever update the last one
eq(foldForInterval(bars, 10.5, T0 + 5 * M, "5m").length, 3, "5m opens a bar");
eq(foldForInterval(day, 12, T0 + 5 * 24 * 3600e3, "1W").length, 2, "1W never appends");
eq(foldForInterval(bars, 12, T0 + 60 * M, "4h")[1].close, 12, "4h updates inside the bar");
eq(foldForInterval(bars, 12, T0 + 5 * 3600e3, "4h"), bars, "4h leaves a new bar to the refresh");

console.log("liveCandle tests passed");
