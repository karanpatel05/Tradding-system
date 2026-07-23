// frontend/src/components/builder/tradesSummary.test.mjs
// Plain-node test (no runner). Run: node frontend/src/components/builder/tradesSummary.test.mjs
import { summarizeTrades } from "./tradesSummary.js";

const eq = (a, b, msg) => {
  const ok = b === null ? a === null : Math.abs(a - b) < 1e-9;
  if (!ok) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};

// Empty + nullish input → all zeros, winRate null.
for (const input of [[], null, undefined]) {
  const s = summarizeTrades(input);
  eq(s.net, 0, "empty net"); eq(s.realized, 0, "empty realized");
  eq(s.unrealized, 0, "empty unrealized"); eq(s.openCount, 0, "empty open");
  eq(s.closedCount, 0, "empty closed"); eq(s.winRate, null, "empty winRate");
}

// Mixed open/closed, one open trade unvalued (last_pnl null → counts as 0).
const s = summarizeTrades([
  { status: "closed", pnl: 100 },
  { status: "closed", pnl: -40 },
  { status: "open", last_pnl: 25 },
  { status: "open", last_pnl: null },
]);
eq(s.realized, 60, "mixed realized");
eq(s.unrealized, 25, "mixed unrealized");
eq(s.net, 85, "mixed net");
eq(s.openCount, 2, "mixed openCount");
eq(s.closedCount, 2, "mixed closedCount");
eq(s.winRate, 0.5, "mixed winRate");

// Win rate all winners.
eq(summarizeTrades([{ status: "closed", pnl: 5 }, { status: "closed", pnl: 10 }]).winRate, 1, "all winners");

console.log("PASS: summarizeTrades");
