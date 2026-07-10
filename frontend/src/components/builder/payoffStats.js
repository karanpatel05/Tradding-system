// frontend/src/components/builder/payoffStats.js
// Pure payoff-curve statistics for the Builder stats row. Curve pnl is dollars (×100
// upstream). Right edge (price → ∞) can be genuinely unbounded; the left edge (price → 0)
// never is — it's extended by linear extrapolation to price 0 instead.
const EPS = 50; // $ per $1 of underlying; marks right edge as infinite

export function computeStats(curve, breakevens = [], spot = null) {
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
  if (sR > EPS) maxProfit = "infinite";
  if (sR < -EPS) maxLoss = "infinite";
  return { maxProfit, maxLoss, breakevens: bes };
}
