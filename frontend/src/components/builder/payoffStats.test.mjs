// frontend/src/components/builder/payoffStats.test.mjs
// Plain-node test (no runner configured). Run: node frontend/src/components/builder/payoffStats.test.mjs
import { computeStats } from "./payoffStats.js";

const eq = (a, b, msg) => {
  const ok = typeof b === "number" ? Math.abs(a - b) < 1e-6 : a === b;
  if (!ok) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};

// Long call K=100, debit $2/share -> flat -$200 left, +$100/$1 slope right.
const longCall = [
  { price: 80, pnl: -200 }, { price: 90, pnl: -200 }, { price: 100, pnl: -200 },
  { price: 110, pnl: 800 }, { price: 120, pnl: 1800 },
];
let s = computeStats(longCall, [102], 100);
eq(s.maxProfit, "infinite", "long call maxProfit infinite");
eq(s.maxLoss, -200, "long call maxLoss = debit");
eq(s.breakevens[0].price, 102, "breakeven price");
eq(s.breakevens[0].movePct, 0.02, "breakeven movePct");

// Short call: mirrored -> bounded profit, infinite loss.
const shortCall = longCall.map((p) => ({ price: p.price, pnl: -p.pnl }));
s = computeStats(shortCall, [102], 100);
eq(s.maxProfit, 200, "short call maxProfit = credit");
eq(s.maxLoss, "infinite", "short call maxLoss infinite");

// Long put K=100, debit $3/share: left edge extrapolates to price 0 -> finite 9700.
const longPut = [
  { price: 60, pnl: 3700 }, { price: 80, pnl: 1700 },
  { price: 100, pnl: -300 }, { price: 120, pnl: -300 },
];
s = computeStats(longPut, [97], 100);
eq(s.maxProfit, 9700, "long put maxProfit via price-0 extrapolation");
eq(s.maxLoss, -300, "long put maxLoss = debit");

// Bounded both sides (calendar-ish hump): both finite, no extrapolation surprises.
const hump = [
  { price: 90, pnl: -100 }, { price: 95, pnl: 150 }, { price: 100, pnl: 400 },
  { price: 105, pnl: 150 }, { price: 110, pnl: -100 },
];
s = computeStats(hump, [93, 107], 100);
eq(s.maxProfit, 400, "hump maxProfit");
// left edge slope = (150-(-100))/5 = +50/$ -> extrapolation to 0 gives -100-50*90 = -4600
eq(s.maxLoss, -4600, "hump maxLoss includes price-0 extrapolation");
eq(s.breakevens.length, 2, "two breakevens");
eq(s.breakevens[0].movePct, -0.07, "downside movePct negative");

// Degenerate inputs never crash.
s = computeStats([], [], null);
eq(s.maxProfit, null, "empty curve -> null");
s = computeStats([{ price: 100, pnl: 0 }], [], null);
eq(s.maxProfit, null, "single point -> null");
s = computeStats(longCall, [102], null);
eq(s.breakevens[0].movePct, null, "no spot -> movePct null");

console.log("ALL PAYOFF-STATS TESTS PASSED");
