// Fixed, reproducible preview sessions. Every timeframe aggregates the same minutes.
const prices = { QQQ: 490, SPY: 560, IWM: 212, NVDA: 140, AAPL: 225, TSLA: 310, MSFT: 410, AMZN: 210 };
export const symbols = Object.keys(prices);
export const intervals = { "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1D": 390 };
const cache = new Map();
function minutes(symbol) {
  let seed = [...symbol].reduce((n, c) => n * 31 + c.charCodeAt(0), 7);
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  let close = prices[symbol];
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
  let ema, value = 0, volume = 0, session;
  return bars.map(bar => {
    if (study.type === "EMA") {
      ema = ema === undefined ? bar.close : ema + 2 / (study.period + 1) * (bar.close - ema);
      return ema;
    }
    if (session !== bar.session) { session = bar.session; value = volume = 0; }
    value += bar.value;
    volume += bar.volume;
    // A session can open with zero-volume bars; use the close until volume arrives.
    return volume ? value / volume : bar.close;
  });
}
