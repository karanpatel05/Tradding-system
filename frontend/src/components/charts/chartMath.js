// Pure chart calculations shared by the chart panes and the right-hand details.
// Bars are {time, session, open, high, low, close, volume, value} oldest first.

const etDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
});

// Trading session (New York calendar date) a bar belongs to.
export const sessionOf = time => etDate.format(new Date(time));

// Normalize feed candles into chart bars: add the session and the price*volume
// sum the VWAP study accumulates.
export function normalizeBars(raw) {
  return raw.map(b => ({
    time: b.time, session: sessionOf(b.time),
    open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume || 0,
    value: (b.high + b.low + b.close) / 3 * (b.volume || 0),
  }));
}

// Merge every `factor` consecutive bars of one session (15m -> 30m or 1h).
export function aggregate(bars, factor) {
  if (factor <= 1) return bars;
  const out = [];
  let count = 0;
  for (const bar of bars) {
    const last = out.at(-1);
    if (!last || last.session !== bar.session || count === factor) {
      out.push({ ...bar });
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
  return out;
}

export function ema(values, period) {
  let e;
  const k = 2 / (period + 1);
  return values.map(v => (e = e === undefined ? v : e + k * (v - e)));
}

// Wilder RSI. The first `period` values are null (not enough history).
export function rsi(values, period = 14) {
  const out = values.map(() => null);
  if (values.length <= period) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    gain += Math.max(d, 0);
    loss += Math.max(-d, 0);
  }
  gain /= period;
  loss /= period;
  const value = () => (loss === 0 ? 100 : 100 - 100 / (1 + gain / loss));
  out[period] = value();
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = value();
  }
  return out;
}

// Indices where the fast line crosses the slow line: "up" (fast moves above) or "down".
export function crossovers(fast, slow) {
  const out = [];
  for (let i = 1; i < fast.length; i++) {
    const before = fast[i - 1] - slow[i - 1], now = fast[i] - slow[i];
    if (before <= 0 && now > 0) out.push({ index: i, direction: "up" });
    else if (before >= 0 && now < 0) out.push({ index: i, direction: "down" });
  }
  return out;
}

// Price series a study reads: close, open, high, low, hl2 or hlc3.
export function sourceValues(bars, source = "close") {
  switch (source) {
    case "open": return bars.map(b => b.open);
    case "high": return bars.map(b => b.high);
    case "low": return bars.map(b => b.low);
    case "hl2": return bars.map(b => (b.high + b.low) / 2);
    case "hlc3": return bars.map(b => (b.high + b.low + b.close) / 3);
    default: return bars.map(b => b.close);
  }
}

// Simple moving average; null until `period` values exist.
export function sma(values, period) {
  let sum = 0;
  return values.map((v, i) => {
    sum += v;
    if (i >= period) sum -= values[i - period];
    return i >= period - 1 ? sum / period : null;
  });
}

// Bollinger Bands: SMA middle and +/- mult population standard deviations.
export function bollinger(values, period = 20, mult = 2) {
  const mid = sma(values, period);
  const upper = [], lower = [];
  values.forEach((_, i) => {
    if (mid[i] === null) { upper.push(null); lower.push(null); return; }
    let sq = 0;
    for (let j = i - period + 1; j <= i; j++) sq += (values[j] - mid[i]) ** 2;
    const sd = Math.sqrt(sq / period);
    upper.push(mid[i] + mult * sd);
    lower.push(mid[i] - mult * sd);
  });
  return { mid, upper, lower };
}

// Donchian channel: highest high and lowest low of the last `period` bars.
export function donchian(bars, period = 20) {
  const upper = [], lower = [], mid = [];
  bars.forEach((_, i) => {
    if (i < period - 1) { upper.push(null); lower.push(null); mid.push(null); return; }
    let hi = -Infinity, lo = Infinity;
    for (let j = i - period + 1; j <= i; j++) { hi = Math.max(hi, bars[j].high); lo = Math.min(lo, bars[j].low); }
    upper.push(hi); lower.push(lo); mid.push((hi + lo) / 2);
  });
  return { upper, lower, mid };
}

// Heikin Ashi candles, used only for drawing; real prices stay everywhere else.
export function heikinAshi(bars) {
  const out = [];
  bars.forEach((b, i) => {
    const close = (b.open + b.high + b.low + b.close) / 4;
    const open = i === 0 ? (b.open + b.close) / 2 : (out[i - 1].open + out[i - 1].close) / 2;
    out.push({ ...b, open, close, high: Math.max(b.high, open, close), low: Math.min(b.low, open, close) });
  });
  return out;
}

// One price<->pixel mapping shared by candles, studies, drawings, crosshair,
// context menu and alert lines. mode: "auto" (linear), "log", or "percent"
// (linear, labelled as % change from `base`).
export function makeScale({ low, high, top, height, mode = "auto", base }) {
  const log = mode === "log" && low > 0;
  const f = log ? Math.log : v => v;
  const inv = log ? Math.exp : v => v;
  const lo = f(low), hi = f(high), span = hi - lo || 1;
  const y = p => top + height * (hi - f(p)) / span;
  const invert = dy => inv(hi - (dy - top) * span / height);
  const label = p => mode === "percent" && base
    ? ((p / base - 1) * 100 >= 0 ? "+" : "") + ((p / base - 1) * 100).toFixed(2) + "%"
    : p.toFixed(2);
  return { y, invert, label, mode: log ? "log" : mode };
}

// Latest session's stats for the details panel: last, change from the prior
// session's close, open, day high/low, volume and VWAP.
export function sessionStats(bars) {
  if (!bars.length) return null;
  const last = bars.at(-1);
  const today = bars.filter(b => b.session === last.session);
  const prior = bars.findLast(b => b.session !== last.session);
  const volume = today.reduce((n, b) => n + b.volume, 0);
  const value = today.reduce((n, b) => n + b.value, 0);
  return {
    last: last.close,
    prevClose: prior?.close ?? null,
    change: prior ? last.close - prior.close : null,
    changePct: prior ? (last.close / prior.close - 1) * 100 : null,
    open: today[0].open,
    high: Math.max(...today.map(b => b.high)),
    low: Math.min(...today.map(b => b.low)),
    volume,
    vwap: volume ? value / volume : null,
  };
}

// Horizontal wheel/trackpad scroll in whole candles. Candles moved follow the distance scrolled
// (pixels ÷ candle width), and the fractional remainder carries to the next event, so a swipe
// made of many small events moves the chart as far as the fingers moved. Positive deltaX scrolls
// toward newer candles. deltaMode 1 = lines (16 px each), 2 = pages (one plot width).
export function wheelBars(carry, deltaX, deltaMode, plotWidth, length) {
  const px = deltaX * (deltaMode === 1 ? 16 : deltaMode === 2 ? plotWidth : 1);
  const total = carry + px * length / Math.max(1, plotWidth);
  const bars = Math.trunc(total + Math.sign(total) * 1e-9);   // absorb float drift (15.9999… → 16)
  return { bars, carry: total - bars };
}

// The visible window of `length` candle slots. `offset` counts candles back from the newest one;
// a negative offset scrolls past it, leaving blank slots on the right for future candles, at most
// half the view (newest candle centred). Returns the clamped offset and bar indices [start, end).
export function viewWindow(total, length, offset) {
  const minOffset = -Math.floor(length / 2);
  const clamped = Math.max(minOffset, Math.min(Math.max(0, total - length), Math.round(offset) || 0));
  const start = Math.max(0, total - clamped - length);
  return { start, end: Math.min(total, total - clamped), offset: clamped, minOffset };
}
