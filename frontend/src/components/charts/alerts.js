import { useEffect, useRef, useState } from "react";

// Price alerts for the chart. They live in this browser, are checked against fresh
// live quotes while the chart is open, and only notify: nothing here places orders.
const KEY = "trading.chart.alerts.v1";
export const CONDITIONS = [
  ["crossing_up", "Crossing up"], ["crossing_down", "Crossing down"], ["crossing", "Crossing"],
  ["above", "Greater than"], ["below", "Less than"],
];
export const conditionLabel = c => (CONDITIONS.find(x => x[0] === c) || [c, c])[1];

// Offset of New York from UTC at `date`, in minutes (e.g. -300 in winter).
function etOffset(date) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(date).map(x => [x.type, x.value]));
  return (Date.UTC(+p.year, p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(date.getTime() / 1000) * 1000) / 60000;
}

// End of the regular New York session (16:00 ET) for today, or the next weekday
// if today's close has passed or it is the weekend.
export function sessionCloseAfter(now = Date.now()) {
  for (let d = 0; d < 7; d++) {
    const probe = new Date(now + d * 86400000);
    const [y, m, day] = new Intl.DateTimeFormat("en-CA", {timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit"})
      .format(probe).split("-").map(Number);
    const noonUtc = new Date(Date.UTC(y, m - 1, day, 17));
    const close = Date.UTC(y, m - 1, day, 16) - etOffset(noonUtc) * 60000;
    const weekday = new Date(Date.UTC(y, m - 1, day)).getUTCDay();
    if (weekday !== 0 && weekday !== 6 && close > now) return close;
  }
  return now + 86400000;
}

// Does moving from `prev` to `price` trigger this alert? A crossing needs a prior
// price, so the first quote after loading never fires a crossing alert.
export function shouldFire(alert, prev, price) {
  const v = alert.value;
  if (price == null || !Number.isFinite(price)) return false;
  switch (alert.condition) {
    case "crossing_up": return prev != null && prev < v && price >= v;
    case "crossing_down": return prev != null && prev > v && price <= v;
    case "crossing": return prev != null && ((prev < v && price >= v) || (prev > v && price <= v));
    case "above": return price > v && !(prev != null && prev > v);
    case "below": return price < v && !(prev != null && prev < v);
    default: return false;
  }
}

function load() {
  try { const a = JSON.parse(localStorage.getItem(KEY)); return Array.isArray(a) ? a : []; } catch { return []; }
}

export function useAlerts() {
  const [alerts, setAlerts] = useState(load);
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(alerts)); } catch { /* private mode */ } }, [alerts]);
  return [alerts, setAlerts];
}

// One shared AudioContext, created from a user click (Create) so autoplay rules allow it.
let audio = null;
export function unlockSound() {
  try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume?.(); } catch { audio = null; }
}
function beep() {
  if (!audio) return;
  try {
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = 880; g.gain.setValueAtTime(0.12, audio.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.5);
    o.connect(g).connect(audio.destination); o.start(); o.stop(audio.currentTime + 0.5);
  } catch { /* audio unavailable */ }
}

// One poll of alert processing (pure). Expiry is checked on every poll, live or not.
// Only fresh live quotes can fire; a stale or missing feed clears the previous prices
// so a recovering feed cannot fire a crossing from an old price.
export function stepAlerts(alerts, prev, quotes, now = Date.now()) {
  const live = quotes.live && !quotes.stale;
  const fired = [];
  const next = alerts.map(a => {
    if (!a.active) return a;
    if (a.expires && now > a.expires) return {...a, active: false, status: "Expired"};
    if (!live) return a;
    const price = quotes.quotes?.[a.symbol]?.price;
    if (!shouldFire(a, prev[a.symbol], price)) return a;
    fired.push({...a, price});
    return a.trigger === "every" ? {...a, lastFired: now, fires: (a.fires || 0) + 1}
      : {...a, active: false, status: "Triggered", lastFired: now, fires: (a.fires || 0) + 1};
  });
  let nextPrev = {};
  if (live) {
    nextPrev = {...prev};
    for (const [s, q] of Object.entries(quotes.quotes || {})) if (q?.price != null) nextPrev[s] = q.price;
  }
  return { alerts: next, fired, prev: nextPrev, changed: next.some((a, i) => a !== alerts[i]) };
}

// Re-arm an alert, giving an end-of-session alert a fresh session close.
export function rearm(alert, now = Date.now()) {
  return {...alert, active: true, status: "Active", expires: alert.expiry === "never" ? null : sessionCloseAfter(now)};
}

export function useAlertEngine(alerts, setAlerts, quotes, notify) {
  const prev = useRef({});
  useEffect(() => {
    const step = stepAlerts(alerts, prev.current, quotes);
    prev.current = step.prev;
    if (step.changed) setAlerts(step.alerts);
    step.fired.forEach(a => { if (a.inApp) notify(a); if (a.sound) beep(); });
  }, [quotes]); // eslint-disable-line react-hooks/exhaustive-deps
}
