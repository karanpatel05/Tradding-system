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
