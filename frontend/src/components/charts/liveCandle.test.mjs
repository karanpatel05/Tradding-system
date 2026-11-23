// Plain-node test (no runner configured). Run: node frontend/src/components/charts/liveCandle.test.mjs
import { foldForInterval, foldLivePrice, rebaseLive } from "./liveCandle.js";

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

// dailyNeverAppends: intervalMs 0 only ever updates the last bar (never appends)
const day = [bar(T0 - 24 * 3600e3, 1, 2, 0.5, 1.5), bar(T0, 10, 11, 9, 10)];
r = foldLivePrice(day, 12, T0 + 3 * 3600e3, 0);
eq(r.length, 2, "daily keeps length"); eq(r[1].close, 12, "daily close updated"); eq(r[1].high, 12, "daily high updated");

// a later day's / week's tick must not touch the previous candle
const D = Date.UTC(2026, 10, 20, 14, 30); // Fri 09:30 ET
const dayBars = [{ ...bar(D, 10, 11, 9, 10), session: "2026-11-20" }];
r = foldForInterval(dayBars, 12, D + 3 * 24 * 3600e3, "1D");
eq(r, dayBars, "1D tick three days later leaves bars unchanged");
eq(foldForInterval(dayBars, 12, D + 3 * 3600e3, "1D")[0].close, 12, "1D same-session tick updates");
eq(foldForInterval(dayBars, 12, D + 3 * 3600e3, "1D").length, 1, "1D never appends");
const wk = [{ ...bar(Date.UTC(2026, 10, 16, 5), 10, 11, 9, 10), session: "2026-11-16" }]; // Mon 00:00 ET
eq(foldForInterval(wk, 12, D, "1W")[0].close, 12, "1W same-week tick updates");
eq(foldForInterval(wk, 12, D + 3 * 24 * 3600e3, "1W"), wk, "1W next-week tick leaves bars unchanged");

// accumulatesTicks: open stays the first live price, high/low accumulate
let live = bars;
for (const [i, p] of [11, 13, 9, 12].entries()) live = foldLivePrice(live, p, T0 + 5 * M + i * 1000, 5 * M);
eq(live.length, 3, "one new bar"); eq(live[2].open, 11, "open is first tick"); eq(live[2].high, 13, "high accumulated");
eq(live[2].low, 9, "low accumulated"); eq(live[2].close, 12, "close is last tick");

// rebaseKeepsLiveBars
const liveBar = live[2];
r = rebaseLive([bar(T0 - 5 * M, 9, 10, 8, 9), bar(T0, 10, 11, 9, 10.2)], live);
eq(r.length, 3, "live bar kept after newer stored bars"); eq(r[2], liveBar, "live bar unchanged"); eq(r[1].close, 10.2, "stored bar wins at same time");
const stored = bar(T0 + 5 * M, 10.4, 10.6, 10.3, 10.5);
r = rebaseLive([bars[0], bars[1], stored], live);
eq(r.length, 3, "stored bar at the same time replaces the live bar"); eq(r[2], stored, "stored bar wins");
eq(rebaseLive(bars, null), bars, "no live state returns stored");

// guards
eq(foldLivePrice([], 1, T0, 5 * M).length, 0, "empty bars untouched");
eq(foldLivePrice(bars, NaN, T0, 5 * M), bars, "bad price ignored");
// foldForInterval: intraday opens bars, 1D/1W/4h only ever update the last one
eq(foldForInterval(bars, 10.5, T0 + 5 * M, "5m").length, 3, "5m opens a bar");
eq(foldForInterval(bars, 12, T0 + 60 * M, "4h")[1].close, 12, "4h updates inside the bar");
eq(foldForInterval(bars, 12, T0 + 5 * 3600e3, "4h"), bars, "4h leaves a new bar to the refresh");

// F1: a 1D bar stamped 09:30 ET moves with a same-day tick; the old 00:00 UTC stamp (previous ET
// day) never did, because foldForInterval saw the tick as another session.
{
  const open0930 = Date.UTC(2026, 10, 20, 14, 30), tick = Date.UTC(2026, 10, 20, 20, 0); // Fri 15:00 ET
  const d1 = [{ ...bar(open0930, 10, 11, 9, 10), session: "2026-11-20" }];
  eq(foldForInterval(d1, 12.5, tick, "1D")[0].close, 12.5, "09:30 ET daily bar moves with a same-day tick");
  const old = [{ ...bar(Date.UTC(2026, 10, 20), 10, 11, 9, 10), session: "2026-11-19" }];
  eq(foldForInterval(old, 12.5, tick, "1D"), old, "00:00 UTC stamp is another ET session (the defect F1 removes)");
}

console.log("liveCandle tests passed");
