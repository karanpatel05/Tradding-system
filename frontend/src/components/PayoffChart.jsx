import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

// Payoff diagram: P&L (dollars) across underlying price at the chosen evaluation date.
export default function PayoffChart({ curve, breakevens = [] }) {
  if (!curve || curve.length === 0) return <div className="muted">No payoff data.</div>;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={curve} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="pnlPos" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#34d399" stopOpacity={0.5} />
            <stop offset="100%" stopColor="#34d399" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#243" />
        <XAxis dataKey="price" tick={{ fontSize: 10 }} minTickGap={40} />
        <YAxis tick={{ fontSize: 10 }} />
        <Tooltip
          contentStyle={{ background: "#131722", border: "1px solid #242c3a", borderRadius: 8 }}
          formatter={(v) => [`$${v}`, "P&L"]}
          labelFormatter={(l) => `Underlying ${l}`}
        />
        <ReferenceLine y={0} stroke="#5c6677" />
        {breakevens.map((be) => (
          <ReferenceLine key={be} x={be} stroke="#818cf8" strokeDasharray="4 4" />
        ))}
        <Area type="monotone" dataKey="pnl" stroke="#34d399" fill="url(#pnlPos)" />
      </AreaChart>
    </ResponsiveContainer>
  );
}
