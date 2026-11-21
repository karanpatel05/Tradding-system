// Fixed, reproducible preview sessions. Every timeframe aggregates the same minutes.
import { bollinger, donchian, ema, sma, sourceValues } from "./chartMath.js";
const prices = { QQQ: 490, SPY: 560, IWM: 212, NVDA: 140, AAPL: 225, TSLA: 310, MSFT: 410, AMZN: 210 };
export const symbols = Object.keys(prices);
export const intervals = { "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1D": 390 };
const cache = new Map();
function minutes(symbol) {
  let seed = [...symbol].reduce((n, c) => n * 31 + c.charCodeAt(0), 7);
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  // Unknown symbols (free typed in search) get a fixed, symbol-derived start price.
  let close = prices[symbol] ?? 40 + ((seed >>> 0) % 460);
  const bars = [];
  for (let day = 0; day < 42; day++) {
    const start = Date.UTC(2026, 9, 5 + day, 13, 30);
    const weekday = new Date(start).getUTCDay();
    if (!weekday || weekday === 6) continue;
    const session = new Date(start).toISOString().slice(0, 10);
    for (let minute = 0; minute < 390; minute++) {
      const open = close;
      close = open * (1 + (random() - 0.497) * 0.002);
      const high = Math.max(open, close) + random() * open * 0.0005;
      const low = Math.min(open, close) - random() * open * 0.0005;
      const volume = 1000 + Math.floor(random() * 16000);
      const time = start + (day >= 27 ? 3600000 : 0) + minute * 60000;
      bars.push({ time, session, open, high, low, close, volume, value: (high + low + close) / 3 * volume });
    }
  }
  return bars;
}
export function getBars(symbol, interval) {
  const key = symbol + interval;
  if (!cache.has(key)) {
    if (!cache.has(symbol)) cache.set(symbol, minutes(symbol));
    const result = [];
    let count = 0;
    for (const bar of cache.get(symbol)) {
      let last = result.at(-1);
      if (!last || last.session !== bar.session || count === intervals[interval]) {
        result.push({ ...bar });
        count = 1;
      } else {
        last.high = Math.max(last.high, bar.high);
        last.low = Math.min(last.low, bar.low);
        last.close = bar.close;
        last.volume += bar.volume;
        last.value += bar.value;
        count++;
      }
    }
    cache.set(key, result);
  }
  return cache.get(key);
}
export function studyValues(bars, study) {
  if (study.type === "EMA") return ema(sourceValues(bars, study.source), study.period);
  if (study.type === "SMA") return sma(sourceValues(bars, study.source), study.period);
  let value = 0, volume = 0, session;
  return bars.map(bar => {
    if (session !== bar.session) { session = bar.session; value = volume = 0; }
    value += bar.value;
    volume += bar.volume;
    // A session can open with zero-volume bars; use the close until volume arrives.
    return volume ? value / volume : bar.close;
  });
}

// Indicator catalogue: defaults and the library's categories.
export const STUDY_DEFAULTS = {
  EMA: { period: 9, source: "close", color: "#f5b84a", width: 1.5, markers: true },
  SMA: { period: 20, source: "close", color: "#5ec8f5", width: 1.5 },
  VWAP: { color: "#9aa7b6", width: 1.5 },
  "Bollinger Bands": { period: 20, mult: 2, source: "close", color: "#b58cff", width: 1 },
  "Donchian Channel": { period: 20, color: "#5ec8f5", width: 1 },
  RSI: { period: 14, color: "#b58cff", width: 1.5 },
  Volume: {},
};
export const STUDY_CATALOG = [
  { type: "EMA", category: "Trend", desc: "Exponential moving average" },
  { type: "SMA", category: "Trend", desc: "Simple moving average" },
  { type: "VWAP", category: "Trend", desc: "Session volume-weighted price" },
  { type: "RSI", category: "Momentum", desc: "Relative strength index, separate pane" },
  { type: "Volume", category: "Volume", desc: "Bar volume under the candles" },
  { type: "Bollinger Bands", category: "Volatility", desc: "SMA with standard-deviation bands" },
  { type: "Donchian Channel", category: "Volatility", desc: "Highest high and lowest low" },
];
// Types that can appear more than once (different lengths).
export const MULTI = new Set(["EMA", "SMA"]);
export const PRICE_STUDIES = new Set(["EMA", "SMA", "VWAP", "Bollinger Bands", "Donchian Channel"]);

export function newStudy(type) {
  const id = globalThis.crypto?.randomUUID?.() || String(Date.now() + Math.random());
  return { id, type, visible: true, ...STUDY_DEFAULTS[type] };
}

export function studyLabel(s) {
  if (s.type === "Bollinger Bands") return `BB ${s.period ?? 20} ${s.mult ?? 2}`;
  if (s.type === "Donchian Channel") return `DC ${s.period ?? 20}`;
  return s.period && s.type !== "VWAP" ? `${s.type} ${s.period}` : s.type;
}

// Lines a price-pane study draws. Values may start with nulls; renderers skip them.
export function studyLines(bars, s) {
  const color = s.color || "#9aa7b6", width = s.width || 1.5;
  if (s.type === "Bollinger Bands") {
    const b = bollinger(sourceValues(bars, s.source), s.period || 20, s.mult ?? 2);
    return [
      { key: "upper", values: b.upper, color, width },
      { key: "mid", values: b.mid, color, width, dash: "4 3" },
      { key: "lower", values: b.lower, color, width },
    ];
  }
  if (s.type === "Donchian Channel") {
    const d = donchian(bars, s.period || 20);
    return [
      { key: "upper", values: d.upper, color, width },
      { key: "mid", values: d.mid, color, width, dash: "4 3" },
      { key: "lower", values: d.lower, color, width },
    ];
  }
  return [{ key: "line", values: studyValues(bars, s), color, width }];
}

// Apply one study's settings to another pane's list. Match by id; EMA/SMA (which can
// appear several times) then match type + length, otherwise a copy is appended so an
// unrelated line is never overwritten. Single-instance types replace their one study.
export function applyStudy(studies, st) {
  let t = studies.findIndex(x => x.id === st.id);
  if (t < 0) t = MULTI.has(st.type)
    ? studies.findIndex(x => x.type === st.type && x.period === st.period)
    : studies.findIndex(x => x.type === st.type);
  return t < 0 ? [...studies, {...st, id: newStudy(st.type).id}] : studies.map((x, j) => j === t ? {...st, id: x.id} : x);
}
