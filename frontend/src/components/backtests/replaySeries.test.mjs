import assert from "node:assert/strict";
import test from "node:test";
import { replaySeries } from "./replaySeries.js";

test("orders sessions by date and matches the replay's traded-session total", () => {
  const sessions = [
    { date: "2026-04-10", status: "closed", pnl: -0.21 },
    { date: "2026-04-06", status: "settled", pnl: 0.11 },
    { date: "2026-04-07", status: "skipped", pnl: null },
    { date: "2026-04-08", status: "failed", pnl: -2 },
    { date: "2026-04-09", status: "settled", pnl: 0 },
    { date: "2026-04-13", status: "closed", pnl: null },
  ];
  const rows = replaySeries(sessions);
  assert.deepEqual(rows.map((r) => r.cumulative), [0.11, 0.11, 0.11, 0.11, -0.1, -0.1]);
  assert.deepEqual(rows.filter((r) => r.traded).map((r) => r.date), ["2026-04-06", "2026-04-09", "2026-04-10"]);
  assert.equal(sessions[0].date, "2026-04-10");
});
