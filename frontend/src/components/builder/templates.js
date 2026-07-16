// frontend/src/components/builder/templates.js
// Pure strategy-template generators. ATM = nearest strike to spot; "step" = index
// step in the expiry's strike list; all outputs are plain leg dicts.
const atmIndex = (strikes, spot) => {
  let best = 0;
  strikes.forEach((s, i) => { if (Math.abs(s - spot) < Math.abs(strikes[best] - spot)) best = i; });
  return best;
};
const at = (strikes, i) => strikes[Math.min(strikes.length - 1, Math.max(0, i))];
const leg = (action, right, strike, expiry, qty = 1) => ({ action, right, strike, expiry, qty });

export function buildTemplate(name, { expiry, strikes, spot }) {
  if (!expiry || !strikes?.length || spot == null) return null;
  const i = atmIndex(strikes, spot);
  switch (name) {
    case "vertical":
      return [leg("BUY", "C", at(strikes, i), expiry), leg("SELL", "C", at(strikes, i + 2), expiry)];
    case "straddle":
      return [leg("BUY", "C", at(strikes, i), expiry), leg("BUY", "P", at(strikes, i), expiry)];
    case "butterfly":
      return [leg("BUY", "C", at(strikes, i - 2), expiry), leg("SELL", "C", at(strikes, i), expiry, 2),
              leg("BUY", "C", at(strikes, i + 2), expiry)];
    case "iron_condor":
      return [leg("SELL", "P", at(strikes, i - 2), expiry), leg("BUY", "P", at(strikes, i - 4), expiry),
              leg("SELL", "C", at(strikes, i + 2), expiry), leg("BUY", "C", at(strikes, i + 4), expiry)];
    case "covered_call":
      return [leg("BUY", "S", 0, "", 100), leg("SELL", "C", at(strikes, i + 2), expiry)];
    default:
      return null;
  }
}

// Directional exposure sign (per-contract; stock counts qty/100). Positive -> long bias.
export function netBias(legs) {
  let n = 0;
  for (const l of legs) {
    const sign = l.action === "BUY" ? 1 : -1;
    if (l.right === "C") n += sign;
    else if (l.right === "P") n -= sign;
    else if (l.right === "S") n += sign * (Number(l.qty) || 0) / 100;
  }
  return n;
}

export function buildHedge(legs, { spot, strikesFor }) {
  if (!legs.length || spot == null) return null;
  const bias = netBias(legs);
  if (bias === 0) return null;
  const optionLegs = legs.filter((l) => l.right !== "S" && l.expiry);
  const expiry = optionLegs.map((l) => l.expiry).sort()[0] || null;
  if (!expiry) return null;
  const strikes = strikesFor(expiry);
  if (!strikes?.length) return null;
  const target = bias > 0 ? spot * 0.95 : spot * 1.05;
  const k = strikes.reduce((a, b) => (Math.abs(b - target) < Math.abs(a - target) ? b : a));
  return leg("BUY", bias > 0 ? "P" : "C", k, expiry);
}
