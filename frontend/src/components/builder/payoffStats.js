// frontend/src/components/builder/payoffStats.js
// Pure payoff-curve statistics for the Builder stats row. Curve pnl is dollars (×100
// upstream). Right edge (price → ∞) can be genuinely unbounded; the left edge (price → 0)
// never is (puts cap at strike) — it's extended by linear extrapolation to price 0 instead.
//
// Two detection paths for right-edge unboundedness:
//
//  1. Leg-aware (preferred — pass `legs`, an array of {action:"BUY"|"SELL",
//     right:"C"|"P", qty}). As price → ∞ only calls matter (puts → 0), so unboundedness
//     is EXACT from net call quantity: netCalls = Σ over call legs of (BUY: +qty,
//     SELL: −qty).
//       netCalls > 0  → net long calls   → maxProfit = "infinite"
//       netCalls < 0  → net short calls  → maxLoss   = "infinite"
//       netCalls === 0 → calls cancel out → both sides bounded on the right.
//     This is exact regardless of the sampled curve window, so it can't be fooled either
//     way: review found that on the backend's narrow default window, bounded spreads can
//     show residual right-edge slope up to ~9 $/$ while genuinely unbounded structures
//     (e.g. short strangles) can show edge slope as low as ~14 $/$ — the ranges overlap,
//     so no curve-slope threshold can classify both correctly. Leg data sidesteps that
//     entirely. When a side is "infinite" per netCalls, that label wins regardless of what
//     the curve extremes say; the other (non-overridden) side is still read off the curve
//     extremes + the price-0 extrapolated point, same as the heuristic path below.
//
//  2. Heuristic (fallback — used only when `legs` is missing/empty, e.g. an unknown/legacy
//     caller). Infers unboundedness from the curve's right-edge slope. This is APPROXIMATE:
//     per the review above, a single slope threshold cannot cleanly separate bounded from
//     unbounded structures on a narrow window. The threshold here is therefore biased SAFE
//     — lower than a "no false positives" threshold would be — so ambiguous cases read
//     "Infinite" more often. That's the safe direction: misreading unbounded risk as bounded
//     is dangerous (a trader sees "Max loss: $X" on an infinite-risk position); misreading
//     bounded risk as unbounded is merely annoying. Callers that have `legs` available
//     should always pass them and use path 1 instead.
const EPS = 10; // heuristic-path-only: $ per $1 of underlying; safe-biased (see above)

function netCallQty(legs) {
  let net = 0;
  for (const leg of legs) {
    if (leg.right !== "C") continue;
    net += leg.action === "BUY" ? leg.qty : -leg.qty;
  }
  return net;
}

export function computeStats(curve, breakevens = [], spot = null, legs = null) {
  const bes = (breakevens || []).map((be) => ({
    price: be,
    movePct: spot ? (be - spot) / spot : null,
  }));
  if (!curve || curve.length < 2) return { maxProfit: null, maxLoss: null, breakevens: bes };

  const n = curve.length;
  const slope = (a, b) => (b.pnl - a.pnl) / (b.price - a.price || 1);
  const sR = slope(curve[n - 2], curve[n - 1]);
  const sL = slope(curve[0], curve[1]);
  // Left edge is always bounded: extend linearly down to price 0.
  const pnlAtZero = curve[0].pnl - sL * curve[0].price;
  const pnls = curve.map((p) => p.pnl).concat([pnlAtZero]);

  let maxProfit = Math.max(...pnls);
  let maxLoss = Math.min(...pnls);

  const haveLegs = Array.isArray(legs) && legs.length > 0;
  if (haveLegs) {
    const netCalls = netCallQty(legs);
    if (netCalls > 0) maxProfit = "infinite";
    else if (netCalls < 0) maxLoss = "infinite";
    // netCalls === 0: calls cancel out, both sides stay bounded — leave curve-derived values.
  } else {
    if (sR > EPS) maxProfit = "infinite";
    if (sR < -EPS) maxLoss = "infinite";
  }

  return { maxProfit, maxLoss, breakevens: bes };
}
