// The replay aggregates only settled/closed sessions with a realized dollar P&L.
export function replaySeries(sessions = []) {
  let totalCents = 0;
  return [...sessions].sort((a, b) => a.date.localeCompare(b.date)).map((row) => {
    const traded = (row.status === "settled" || row.status === "closed") && Number.isFinite(row.pnl);
    if (traded) totalCents += Math.round(row.pnl * 100);
    return { ...row, traded, cumulative: totalCents / 100 };
  });
}
