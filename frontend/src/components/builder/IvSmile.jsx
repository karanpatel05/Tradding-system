// frontend/src/components/builder/IvSmile.jsx
// Implied-vol smile for the selected expiry: strike on x, IV% on y (bisection-inverted
// from live mids server-side). Fetches ONLY on expiry change (debounced) + manual
// refresh — never on every quote tick, since each fetch fans out ~20 option quote
// subscriptions server-side.
import { useEffect, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../../api.js";

export default function IvSmile({ underlying, expiry }) {
  const [data, setData] = useState(null); // {spot, points}
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = () => {
    if (!underlying?.trim() || !expiry) return;
    setBusy(true);
    setErr(null);
    api
      .builderIvSmile(underlying.trim(), expiry)
      .then(setData)
      .catch((e) => setErr(e.message))
      .finally(() => setBusy(false));
  };

  const loadRef = useRef(load);
  loadRef.current = load;

  // Debounced fetch on expiry (or underlying) change only.
  useEffect(() => {
    if (!underlying?.trim() || !expiry) {
      setData(null);
      return undefined;
    }
    const t = setTimeout(() => loadRef.current(), 800);
    return () => clearTimeout(t);
  }, [underlying, expiry]);

  if (!expiry) return null;

  const points = (data?.points || []).map((p) => ({ ...p, ivPct: p.iv * 100 }));
  const spot = data?.spot;

  return (
    <section className="card iv-smile-card">
      <div className="card-head">
        <h2>IV Smile</h2>
        <div className="row gap center">
          {busy && <span className="muted small">Loading…</span>}
          <button className="btn ghost tiny" onClick={load} disabled={busy}>
            Refresh
          </button>
        </div>
      </div>
      {err && <div className="banner ok">{err}</div>}
      {points.length < 2 ? (
        <div className="muted">Insufficient quotes for a smile.</div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart
            data={points}
            margin={{ top: 12, right: 12, bottom: 0, left: 0 }}
          >
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="var(--border)"
              vertical={false}
            />
            <XAxis
              dataKey="strike"
              type="number"
              domain={["dataMin", "dataMax"]}
              tick={{ fontSize: 10 }}
              minTickGap={30}
              tickFormatter={(v) => `$${v}`}
            />
            <YAxis
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `${v.toFixed(0)}%`}
              width={48}
            />
            <Tooltip
              contentStyle={{
                background: "var(--card)",
                border: "1px solid var(--border)",
                borderRadius: 8,
              }}
              formatter={(v) => [`${v.toFixed(2)}%`, "IV"]}
              labelFormatter={(l) => `Strike $${l}`}
            />
            {spot != null && (
              <ReferenceLine
                x={spot}
                stroke="var(--muted)"
                strokeOpacity={0.7}
                label={{
                  value: "Spot",
                  position: "top",
                  fill: "#8b93a7",
                  fontSize: 10,
                }}
              />
            )}
            <Line
              type="monotone"
              dataKey="ivPct"
              stroke="var(--accent)"
              strokeWidth={2}
              dot={{ r: 3, fill: "var(--accent)" }}
              isAnimationActive
              animationDuration={300}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}
