// frontend/src/components/builder/payoffStats.test.mjs
// Plain-node test (no runner configured). Run: node frontend/src/components/builder/payoffStats.test.mjs
import { computeStats } from "./payoffStats.js";

const eq = (a, b, msg) => {
  const ok = typeof b === "number" ? Math.abs(a - b) < 1e-6 : a === b;
  if (!ok) { console.error(`FAIL: ${msg} — got ${a}, want ${b}`); process.exit(1); }
};

// Long call K=100, debit $2/share -> flat -$200 left, +$100/$1 slope right.
// legs: net long 1 call -> netCalls = +1 -> maxProfit exact-infinite.
const longCall = [
  { price: 80, pnl: -200 }, { price: 90, pnl: -200 }, { price: 100, pnl: -200 },
  { price: 110, pnl: 800 }, { price: 120, pnl: 1800 },
];
const longCallLegs = [{ action: "BUY", right: "C", qty: 1 }];
let s = computeStats(longCall, [102], 100, longCallLegs);
eq(s.maxProfit, "infinite", "long call maxProfit infinite (leg-aware)");
eq(s.maxLoss, -200, "long call maxLoss = debit");
eq(s.breakevens[0].price, 102, "breakeven price");
eq(s.breakevens[0].movePct, 0.02, "breakeven movePct");

// Short call: mirrored -> bounded profit, infinite loss.
// legs: net short 1 call -> netCalls = -1 -> maxLoss exact-infinite.
const shortCall = longCall.map((p) => ({ price: p.price, pnl: -p.pnl }));
const shortCallLegs = [{ action: "SELL", right: "C", qty: 1 }];
s = computeStats(shortCall, [102], 100, shortCallLegs);
eq(s.maxProfit, 200, "short call maxProfit = credit");
eq(s.maxLoss, "infinite", "short call maxLoss infinite (leg-aware)");

// Long put K=100, debit $3/share: left edge extrapolates to price 0 -> finite 9700.
// legs: a put leg contributes 0 to netCalls -> netCalls = 0 -> both bounded on the right,
// curve-derived values stand unmodified.
const longPut = [
  { price: 60, pnl: 3700 }, { price: 80, pnl: 1700 },
  { price: 100, pnl: -300 }, { price: 120, pnl: -300 },
];
const longPutLegs = [{ action: "BUY", right: "P", qty: 1 }];
s = computeStats(longPut, [97], 100, longPutLegs);
eq(s.maxProfit, 9700, "long put maxProfit via price-0 extrapolation");
eq(s.maxLoss, -300, "long put maxLoss = debit");

// Bounded both sides (calendar-ish hump), FLAT edges so hand arithmetic is trivial, plus
// legs for a genuinely bounded 2-leg structure (long call + short call, same qty ->
// netCalls = 1 - 1 = 0 -> both sides bounded regardless of any residual curve slope).
//
// Hand computation:
//   curve points: (85,-100) (90,-100) (95,150) (100,400) (105,150) (110,-100) (115,-100)
//   left edge slope  sL = (curve[1].pnl - curve[0].pnl) / (90-85) = (-100 - -100) / 5 = 0
//   pnlAtZero = curve[0].pnl - sL * curve[0].price = -100 - 0*85 = -100
//   right edge slope sR = (curve[-1].pnl - curve[-2].pnl) / (115-110) = (-100 - -100)/5 = 0
//   pnls (curve values + pnlAtZero) = [-100,-100,150,400,150,-100,-100,  -100]
//   maxProfit = max(pnls) = 400
//   maxLoss   = min(pnls) = -100  (matches the flat left/right value, as expected — a flat
//               edge extrapolated to price 0 stays at that same flat value)
const hump = [
  { price: 85, pnl: -100 }, { price: 90, pnl: -100 }, { price: 95, pnl: 150 },
  { price: 100, pnl: 400 }, { price: 105, pnl: 150 }, { price: 110, pnl: -100 },
  { price: 115, pnl: -100 },
];
const humpLegs = [
  { action: "BUY", right: "C", qty: 1 },
  { action: "SELL", right: "C", qty: 1 },
];
s = computeStats(hump, [93, 107], 100, humpLegs);
eq(s.maxProfit, 400, "hump maxProfit (bounded, leg-aware)");
eq(s.maxLoss, -100, "hump maxLoss (bounded, leg-aware) — see hand computation above");
eq(s.breakevens.length, 2, "two breakevens");
eq(s.breakevens[0].movePct, -0.07, "downside movePct negative");

// Review's counterexample: short-strangle-like curve with a SHALLOW right-edge slope
// (-20 $/$ — well within a plausible "looks almost bounded" range on a narrow window) but
// genuinely unbounded loss (short call + short put). The leg rule must override the shallow
// slope: netCalls = -1 (short call) + 0 (put doesn't count) = -1 -> maxLoss infinite.
//   right edge slope = (100 - 300) / (120-110) = -20
const strangle = [
  { price: 80, pnl: 300 }, { price: 90, pnl: 300 }, { price: 100, pnl: 500 },
  { price: 110, pnl: 300 }, { price: 120, pnl: 100 },
];
const strangleLegs = [
  { action: "SELL", right: "C", qty: 1 },
  { action: "SELL", right: "P", qty: 1 },
];
s = computeStats(strangle, [], null, strangleLegs);
eq(s.maxLoss, "infinite", "short strangle maxLoss infinite (leg rule overrides shallow slope)");

// Without legs (heuristic path), the same curve must ALSO read maxLoss infinite: slope
// -20 < -10 (safe-biased threshold) catches it too, so the fallback path agrees here.
s = computeStats(strangle, [], null);
eq(s.maxLoss, "infinite", "short strangle maxLoss infinite (heuristic path, no legs)");

// Bounded credit-spread heuristic case WITHOUT legs: mild residual right-edge slope (+5,
// below the safe threshold of 10) must NOT be over-flagged as infinite.
//   right edge slope = (250 - 200) / (110-100) = 5
const creditSpreadHeuristic = [
  { price: 80, pnl: 200 }, { price: 90, pnl: 200 }, { price: 100, pnl: 200 }, { price: 110, pnl: 250 },
];
s = computeStats(creditSpreadHeuristic, [], null);
eq(s.maxProfit, 250, "mild +5 slope, no legs -> maxProfit finite (not over-flagged)");
eq(typeof s.maxProfit, "number", "maxProfit is a finite number, not the string \"infinite\"");

// Empty legs array behaves like "no legs" (heuristic path), not like a 0-call structure.
s = computeStats(strangle, [], null, []);
eq(s.maxLoss, "infinite", "empty legs array falls back to heuristic path");

// Degenerate inputs never crash.
s = computeStats([], [], null);
eq(s.maxProfit, null, "empty curve -> null");
s = computeStats([{ price: 100, pnl: 0 }], [], null);
eq(s.maxProfit, null, "single point -> null");
s = computeStats(longCall, [102], null);
eq(s.breakevens[0].movePct, null, "no spot -> movePct null");

console.log("ALL PAYOFF-STATS TESTS PASSED");
