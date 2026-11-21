// Plain-node test. Run: node frontend/src/components/charts/alerts.test.mjs
import { rearm, sessionCloseAfter, shouldFire, stepAlerts } from "./alerts.js";

const eq = (a, b, msg) => { if (a !== b) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); } };
const up = { condition: "crossing_up", value: 100 };
eq(shouldFire(up, null, 101), false, "first quote never fires a crossing");
eq(shouldFire(up, 99, 100), true, "crossing up fires at the level");
eq(shouldFire(up, 101, 102), false, "already above does not fire");
eq(shouldFire({ condition: "crossing_down", value: 100 }, 101, 99), true, "crossing down");
eq(shouldFire({ condition: "crossing", value: 100 }, 101, 99), true, "crossing either way (down)");
eq(shouldFire({ condition: "crossing", value: 100 }, 99, 101), true, "crossing either way (up)");
eq(shouldFire({ condition: "above", value: 100 }, null, 101), true, "greater than fires when already above at load");
eq(shouldFire({ condition: "above", value: 100 }, 101, 102), false, "greater than fires once per entry into the zone");
eq(shouldFire({ condition: "below", value: 100 }, 101, 99), true, "less than");
eq(shouldFire(up, 99, null), false, "missing price never fires");

// Thu 20 Nov 2026 15:00 UTC = 10:00 ET (EST). Close is 21:00 UTC.
eq(sessionCloseAfter(Date.UTC(2026, 10, 20, 15)), Date.UTC(2026, 10, 20, 21), "close is 16:00 New York time");
// After the close on Friday 20 Nov... (Fri 21:30 UTC) -> Monday 23 Nov 21:00 UTC.
eq(sessionCloseAfter(Date.UTC(2026, 10, 20, 21, 30)), Date.UTC(2026, 10, 23, 21), "after Friday close rolls to Monday");
// Summer (EDT): 1 Jul 2026 14:00 UTC -> 20:00 UTC.
eq(sessionCloseAfter(Date.UTC(2026, 6, 1, 14)), Date.UTC(2026, 6, 1, 20), "daylight time close is 20:00 UTC");
// One poll at a time through stepAlerts.
const NOW = Date.UTC(2026, 10, 20, 15);
const base = { id: "a", symbol: "QQQ", condition: "crossing_up", value: 100, active: true, trigger: "once", expires: NOW + 3600e3 };
const live = p => ({ live: true, stale: false, quotes: { QQQ: { price: p } } });
let st = stepAlerts([base], {}, live(101), NOW);
eq(st.fired.length, 0, "first live quote does not fire a crossing");
eq(st.prev.QQQ, 101, "previous price recorded");
st = stepAlerts([base], { QQQ: 99 }, live(100.5), NOW);
eq(st.fired.length, 1, "once fires on the crossing");
eq(st.alerts[0].active, false, "once turns the alert off");
eq(st.alerts[0].status, "Triggered", "once status is Triggered");
const every = { ...base, trigger: "every" };
st = stepAlerts([every], { QQQ: 99 }, live(101), NOW);
eq(st.alerts[0].active, true, "every time stays on");
eq(st.alerts[0].fires, 1, "every time counts fires");
st = stepAlerts([base], { QQQ: 99 }, { live: false, stale: false, quotes: { QQQ: { price: 105 } } }, NOW + 7200e3);
eq(st.alerts[0].active, false, "expiry turns the alert off even with no live feed");
eq(st.alerts[0].status, "Expired", "expired status");
st = stepAlerts([base], { QQQ: 99 }, { live: true, stale: true, quotes: { QQQ: { price: 105 } } }, NOW);
eq(st.fired.length, 0, "stale quotes never fire");
eq(Object.keys(st.prev).length, 0, "stale feed clears previous prices");
st = stepAlerts([base], st.prev, live(105), NOW);
eq(st.fired.length, 0, "recovering feed does not fire a crossing from an old price");
eq(stepAlerts([{ ...base, active: false }], { QQQ: 99 }, live(101), NOW).changed, false, "inactive alerts are left alone");
eq(rearm({ ...base, active: false, expiry: "session", expires: NOW - 1 }, NOW).expires, Date.UTC(2026, 10, 20, 21), "re-arm gives a fresh session close");
eq(rearm({ ...base, active: false, expiry: "never", expires: null }, NOW).expires, null, "re-arm keeps never-expiring alerts open");

console.log("alerts: all tests passed");
