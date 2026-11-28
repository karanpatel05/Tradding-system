// Pure chart calculations shared by the chart panes and the right-hand details.
// Bars are {time, session, open, high, low, close, volume, value} oldest first.

const etDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
});

// Trading session (New York calendar date) a bar belongs to.
export const sessionOf = time => etDate.format(new Date(time));

// Normalize feed candles into chart bars: add the session and the price*volume
// sum the VWAP study accumulates.
export function normalizeBars(raw, feed) {
  return raw.map(b => {
    // DXLink uses midnight UTC to identify a daily candle's trading date.
    const time = feed === "1d" && b.time % 86400000 === 0
      ? dailyStart(new Date(b.time).toISOString().slice(0, 10)) : b.time;
    return { time, session: sessionOf(time),
      open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume || 0,
      value: (b.high + b.low + b.close) / 3 * (b.volume || 0) };
  });
}

// Intraday buckets follow the 09:30 New York open. An hourly candle starts on
// the half-hour in both EST and EDT, even if some of its 15m candles are missing.
function intradayStart(time, minutes) {
  const width = minutes * 60000;
  const offset = minutes === 60 ? 30 * 60000 : 0;
  return Math.floor((time - offset) / width) * width + offset;
}

// Merge 15m history into the same 30m or hourly buckets used for trades.
export function aggregate(bars, factor) {
  if (factor <= 1) return bars;
  const out = [];
  for (const bar of bars) {
    const time = intradayStart(bar.time, factor * 15);
    const last = out.at(-1);
    if (!last || last.time !== time || last.session !== bar.session) {
      out.push({ ...bar, time });
    } else {
      last.high = Math.max(last.high, bar.high);
      last.low = Math.min(last.low, bar.low);
      last.close = bar.close;
      last.volume += bar.volume;
      last.value += bar.value;
    }
  }
  return out;
}

const minutesByInterval = { "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60 };

export function barIndexAt(bars, time) {
  let lo = 0, hi = bars.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].time <= time) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

// A null end time follows the latest candle. Panning stores a timestamp so
// incoming candles and history backfills leave the viewed time range in place.
export function chartWindow(bars, view) {
  const length = Math.min(bars.length, Math.max(10, view.length));
  const anchorEnd = view.endTime == null ? bars.length : barIndexAt(bars, view.endTime) + 1;
  const end = Math.max(length, anchorEnd);
  return { length, offset: bars.length - end, end, start: end - length };
}

function dailyStart(session) {
  // New York midnight falls at either 04:00 or 05:00 UTC.
  const summerMidnight = Date.parse(session + "T04:00:00Z");
  const midnight = sessionOf(summerMidnight) === session ? summerMidnight : summerMidnight + 3600000;
  return midnight + (9 * 60 + 30) * 60000;
}

// Fold every print before painting. One array copy per batch, plus one new
// object per changed bar; REST history and earlier React state stay immutable.
export function applyTradeTicks(bars, ticks, interval, pendingVolume = new Map()) {
  let out = bars;
  let bar;
  for (const { time, barTime = time, price, size, updatesPrice } of ticks) {
    if (!Number.isFinite(time) || time <= 0 || !Number.isFinite(price) || price <= 0) continue;
    const tail = out.at(-1);
    const session = interval === "1D" ? sessionOf(barTime) : null;
    let bucket = interval === "1D"
      ? (tail?.session === session ? tail.time : dailyStart(session))
      : intradayStart(barTime, minutesByInterval[interval]);
    let index = out.length - 1;
    if (tail && bucket < tail.time) {
      // A late print can still change an earlier candle's range and volume.
      index = out.findLastIndex(b => interval === "1D" ? b.session <= session : b.time <= bucket);
      if (interval === "1D" && out[index]?.session === session) bucket = out[index].time;
    }
    const last = out[index];
    const newBar = !last || bucket > last.time;
    const shares = Number.isFinite(size) && size > 0 ? size : 0;
    const pending = pendingVolume.get(bucket);
    if (newBar && updatesPrice === false) {
      pendingVolume.set(bucket, { volume: (pending?.volume ?? 0) + shares,
        value: (pending?.value ?? 0) + price * shares });
      continue;
    }
    if (out === bars) out = bars.slice();
    if (newBar) {
      bar = { time: bucket, session: session ?? sessionOf(bucket), open: price,
        high: price, low: price, close: price, volume: 0, value: 0, firstTradeTime: time };
      out.splice(index + 1, 0, bar);
    } else if (last !== bar) {
      bar = { ...last };
      out[index] = bar;
    }
    if (updatesPrice !== false) {
      bar.high = Math.max(bar.high, price);
      bar.low = Math.min(bar.low, price);
      if (time < bar.firstTradeTime) {
        bar.firstTradeTime = time;
        bar.open = price;
      }
      if (time >= (bar.lastTradeTime ?? 0) && (newBar || last === tail || bar.lastTradeTime != null)) {
        bar.lastTradeTime = time;
        bar.close = price;
      }
    }
    bar.volume += shares + (pending?.volume ?? 0);
    bar.value += price * shares + (pending?.value ?? 0);
    if (pending) pendingVolume.delete(bucket);
    bar.hasTrades = true;
  }
  return out;
}

// A history request may return after several trades. Completed feed candles
// are authoritative. Keep newer tick-built candles and reconcile the overlapping
// candle without adding the same trades to its reported volume a second time.
export function reconcileHistory(history, liveBars, interval, keepTradeClose = true, pendingVolume = new Map()) {
  const head = history.at(-1);
  if (!head) return liveBars;
  const out = history.slice();
  // Some buckets may have received only volume-eligible prints. Once history
  // supplies the bar, reconcile its volume without creating a synthetic price.
  for (const [time, pending] of pendingVolume) {
    if (time > head.time) continue;
    const i = out.findIndex(b => b.time === time);
    if (i >= 0 && pending.volume > out[i].volume) {
      out[i] = { ...out[i], volume: pending.volume, value: pending.value, hasTrades: true };
    }
    pendingVolume.delete(time);
  }
  for (const bar of liveBars) {
    if (!bar.hasTrades) continue;
    const same = interval === "1D" ? bar.session === head.session : bar.time === head.time;
    if (same) {
      const useLocalVolume = bar.volume >= head.volume;
      out[out.length - 1] = { ...head, high: Math.max(head.high, bar.high),
        low: Math.min(head.low, bar.low), close: keepTradeClose && bar.lastTradeTime != null ? bar.close : head.close,
        volume: Math.max(head.volume, bar.volume), value: useLocalVolume ? bar.value : head.value,
        lastTradeTime: bar.lastTradeTime, hasTrades: true };
    } else if (bar.time > head.time) {
      out.push(bar);
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
