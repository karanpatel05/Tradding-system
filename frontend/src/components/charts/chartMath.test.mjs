// Plain-node test (no runner configured). Run: node frontend/src/components/charts/chartMath.test.mjs
import { aggregate, applyTradeTicks, bollinger, chartWindow, crossovers, donchian, ema, heikinAshi, makeScale, normalizeBars, reconcileHistory, rsi, sessionOf, sessionStats, sma, sourceValues } from "./chartMath.js";
import { applyStudy, studyValues } from "./chartData.js";

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

// All ticks in a display frame contribute to OHLCV even when the price returns
// to its starting level, including fractional shares and a delayed print.
const start = raw[0].time;
const sale = (seconds, price, size = 1) => ({ time: start + seconds * 1000, price, size });
const live = applyTradeTicks([], [sale(15, 101), sale(35, 103, 0.5), sale(35, 102),
  sale(60, 104, 3), sale(5, 100), sale(50, 101)], "1m");
eq(live.length, 2, "a new minute rolls over");
eq(live[0].open, 100, "earliest observed trade opens the candle");
eq(live[0].close, 101, "last trade in first minute closes that minute");
eq(live[0].high, 103, "intrabracket high is retained");
eq(live[0].low, 100, "intrabracket low is retained");
eq(live[0].volume, 4.5, "every trade contributes its size once");
eq(live[0].value, 455.5, "live VWAP numerator uses traded value");
eq(live[1].close, 104, "late trade does not rewind latest price");
const ticked = applyTradeTicks(bars.slice(0, 1), [sale(10, 110, 2)], "1m");
eq(ticked[0].open, 100, "history retains open");
eq(ticked[0].close, 110, "live trade updates close");
eq(ticked[0].volume, 12, "live trade updates volume");
eq(bars[0].volume, 10, "cached feed bars remain immutable");
const merged = reconcileHistory(bars.slice(0, 1), [...ticked, live[1]], "1m");
eq(merged[0].close, 110, "late REST response cannot undo observed close");
eq(merged[0].volume, 12, "overlapping snapshot never double-counts live volume");
eq(merged[1].close, 104, "a response ending earlier preserves new tick-built candles");
eq(reconcileHistory(bars.slice(0, 1), ticked, "1m", false)[0].close, 101, "offline refresh can update last price");
eq(aggregate([bars[0], bars[4]], 4).length, 2, "missing intervals cannot extend an hourly bar");
eq(applyTradeTicks([], [sale(3599, 101), sale(3600, 102)], "1h")[1].time,
  start + 3600e3, "hourly trade buckets align to the market open");
const daily = applyTradeTicks([bars[0]], [sale(900, 110),
  { time: nextDay[0].time, price: 103, size: 7 }], "1D");
eq(daily.length, 2, "daily merges existing history by NY date before rolling");
eq(daily[0].close, 110, "same-session daily candle updates");
eq(daily[1].session, "2026-11-23", "new NY session opens a daily bar");
eq(daily[1].time, Date.UTC(2026, 10, 23, 14, 30), "winter daily bar starts at 09:30 EST");
eq(applyTradeTicks([], [{ time: Date.UTC(2026, 6, 1, 14), price: 103, size: 2 }], "1D")[0].time,
  Date.UTC(2026, 6, 1, 13, 30), "summer daily bar starts at 09:30 EDT");
const rawDaily = normalizeBars([{ ...raw[0], time: Date.UTC(2026, 10, 20) }], "1d");
eq(rawDaily[0].session, "2026-11-20", "DXLink midnight UTC stamp means the US trading date");
eq(applyTradeTicks(rawDaily, [sale(50, 105)], "1D").length, 1, "same-day trade updates the daily history candle");
const pending = new Map();
const oddLot = { ...sale(1, 500, 0.25), updatesPrice: false };
const waiting = applyTradeTicks([], [oddLot], "1m", pending);
eq(waiting.length, 0, "volume-only trade cannot set a candle price");
const oddLots = applyTradeTicks(waiting, [sale(2, 100), { ...oddLot, time: start + 3000 }], "1m", pending);
eq(oddLots[0].volume, 1.5, "volume-only trades before and after the first eligible price count");
eq(oddLots[0].high, 100, "volume-only trades do not set the high");
eq(oddLots[0].close, 100, "volume-only trades do not set the close");
eq(pending.size, 0, "pending volume clears once consumed");
const earlyClose = Date.UTC(2026, 10, 27, 18); // 13:00 ET
const closing = applyTradeTicks([], [
  { time: earlyClose - 1000, price: 103, size: 2 },
  { time: earlyClose, barTime: earlyClose - 1, price: 104, size: 3 },
], "1m");
eq(closing.length, 1, "closing print lands on the final minute of an early-close session");
eq(closing[0].time, earlyClose - 60000, "no 13:00 candle follows the early close");
eq(closing[0].volume, 5, "final minute includes closing print size");
eq(closing[0].lastTradeTime, earlyClose, "closing trade keeps its exchange timestamp");
const scrollBars = Array.from({ length: 30 }, (_, time) => ({ time }));
const panned = { length: 10, endTime: 22 };
eq(chartWindow(scrollBars, { length: 10, endTime: null }).end, 30, "latest view follows appended bars");
eq(chartWindow(scrollBars.slice(0,-1), panned).start, 13, "panned view starts at chosen time");
eq(chartWindow(scrollBars, panned).start, 13, "new candle does not move a panned view");
eq(chartWindow([{time:-1}, ...scrollBars], panned).end, 24, "backfill preserves the visible time anchor");

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

// SMA, Bollinger, Donchian, Heikin Ashi, sources and the shared scale.
const s3 = sma([1, 2, 3, 4], 3);
eq(s3[1], null, "sma is null before the period");
eq(s3[2], 2, "sma of 1,2,3");
eq(s3[3], 3, "sma rolls forward");
const bb = bollinger([2, 4, 4, 4, 5, 5, 7, 9], 8, 2);
eq(bb.mid[7], 5, "bollinger middle is the sma");
eq(bb.upper[7], 9, "bollinger upper = mid + 2 * population sd (sd 2)");
eq(bb.lower[7], 1, "bollinger lower = mid - 2 * sd");
eq(bb.upper[6], null, "bollinger bands null before the period");
const dc = donchian(bars, 3);
eq(dc.upper[1], null, "donchian null before the period");
eq(dc.upper[2], 104, "donchian upper is the highest high");
eq(dc.lower[2], 99, "donchian lower is the lowest low");
eq(dc.mid[2], 101.5, "donchian middle");
const ha = heikinAshi(bars);
eq(ha[0].close, (100 + 102 + 99 + 101) / 4, "heikin ashi close is the OHLC average");
eq(ha[1].open, (ha[0].open + ha[0].close) / 2, "heikin ashi open is the prior HA midpoint");
eq(ha[0].high >= ha[0].open && ha[0].high >= ha[0].close, true, "heikin ashi high covers body");
eq(sourceValues(bars, "hl2")[0], 100.5, "hl2 source");
eq(sourceValues(bars, "open")[0], 100, "open source");
const lin = makeScale({ low: 100, high: 200, top: 10, height: 100 });
eq(lin.y(200), 10, "linear scale maps high to top");
eq(lin.y(150), 60, "linear scale midpoint");
eq(lin.invert(60), 150, "linear invert round-trips");
const lg = makeScale({ low: 100, high: 400, top: 0, height: 100, mode: "log" });
eq(lg.y(200), 50, "log scale puts the geometric midpoint at half height");
eq(lg.invert(lg.y(321)), 321, "log invert round-trips");
eq(makeScale({ low: 1, high: 2, top: 0, height: 1, mode: "percent", base: 100 }).label(105), "+5.00%", "percent labels from base");

// Apply-to-all never overwrites an unrelated EMA/SMA line.
const pane2 = [{ id: "fast", type: "EMA", period: 9 }, { id: "r", type: "RSI", period: 14 }];
const ema50 = { id: "new1", type: "EMA", period: 50, color: "#f0657a" };
const applied = applyStudy(pane2, ema50);
eq(applied.length, 3, "EMA 50 is appended when the pane has no EMA 50");
eq(applied[0].period, 9, "existing EMA 9 is untouched");
eq(applied[2].period, 50, "appended copy has the new length");
eq(applied[2].id !== "new1", true, "appended copy gets its own id");
eq(applyStudy(applied, { ...ema50, id: "other", color: "#fff" }).length, 3, "a matching EMA 50 is updated, not duplicated");
eq(applyStudy(pane2, { id: "x", type: "EMA", period: 9, color: "#abc" })[0].color, "#abc", "same type + length updates in place");
eq(applyStudy(pane2, { id: "fast", type: "EMA", period: 12 })[0].period, 12, "id match updates that study");
eq(applyStudy(pane2, { id: "zz", type: "RSI", period: 21 })[1].period, 21, "single-instance types replace their study");
eq(applyStudy(pane2, { id: "zz", type: "RSI", period: 21 })[1].id, "r", "replaced study keeps the pane's id");

console.log("chartMath: all tests passed");
