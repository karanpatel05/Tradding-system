// Portfolio totals across saved Builder trades. Pure + null-safe: the caller feeds
// the raw /api/builder/trades list (open trades carry last_pnl from the monitor;
// closed trades carry final pnl).
export function summarizeTrades(trades) {
  const list = Array.isArray(trades) ? trades : [];
  let realized = 0, unrealized = 0, openCount = 0, closedCount = 0, winners = 0;
  for (const t of list) {
    if (t.status === "closed") {
      closedCount += 1;
      const p = t.pnl || 0;
      realized += p;
      if (p > 0) winners += 1;
    } else {
      openCount += 1;
      unrealized += t.last_pnl || 0;
    }
  }
  return {
    net: realized + unrealized,
    realized,
    unrealized,
    openCount,
    closedCount,
    winRate: closedCount === 0 ? null : winners / closedCount,
  };
}
