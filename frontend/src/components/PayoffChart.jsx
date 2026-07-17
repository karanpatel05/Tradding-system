import {
  Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

// Payoff diagram: P&L (dollars) across underlying price at the chosen evaluation date.
// Green above zero / red below via a gradient split at the zero crossing; dotted lines
// at leg strikes, blue line + label at each breakeven, thin marker at spot. Optional
// `deltaCurve` ([{price, delta}]) draws the net-delta line on its own hidden right axis.
const splitOffset = (data) => {
  const max = Math.max(...data.map((d) => d.pnl));
  const min = Math.min(...data.map((d) => d.pnl));
  if (max <= 0) return 0;
  if (min >= 0) return 1;
  return max / (max - min);
};

export default function PayoffChart({
  curve, breakevens = [], strikes = [], spot = null, window = null,
  height = 380, animate = true, deltaCurve = null,
}) {
  if (!curve || curve.length === 0) return <div className="muted">No payoff data.</div>;
  const data = window ? curve.filter((p) => p.price >= window[0] && p.price <= window[1]) : curve;
  if (data.length < 2) return <div className="muted">No payoff data in range.</div>;
  const off = splitOffset(data);
  const inWin = (x) => x >= data[0].price && x <= data[data.length - 1].price;
  // Clip the delta overlay to the visible window so it can't stretch the x domain.
  const deltaData = deltaCurve?.length
    ? deltaCurve.filter((p) => inWin(p.price)) : null;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 18, right: 12, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset={0} stopColor="#22c55e" stopOpacity={0.45} />
            <stop offset={off} stopColor="#22c55e" stopOpacity={0.04} />
            <stop offset={off} stopColor="#ef4444" stopOpacity={0.06} />
            <stop offset={1} stopColor="#ef4444" stopOpacity={0.5} />
          </linearGradient>
          <linearGradient id="pnlStroke" x1="0" y1="0" x2="0" y2="1">
            <stop offset={0} stopColor="#22c55e" />
            <stop offset={off} stopColor="#22c55e" />
            <stop offset={off} stopColor="#ef4444" />
            <stop offset={1} stopColor="#ef4444" />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#243" vertical={false} />
        <XAxis dataKey="price" type="number" domain={["dataMin", "dataMax"]}
          tick={{ fontSize: 10 }} minTickGap={40}
          tickFormatter={(v) => `$${Math.round(v)}`} />
        <YAxis tick={{ fontSize: 10 }} tickFormatter={(v) => `$${v.toLocaleString()}`} width={64} />
        <Tooltip
          contentStyle={{ background: "#131722", border: "1px solid #242c3a", borderRadius: 8 }}
          formatter={(v) => [`$${v.toLocaleString()}`, "P&L"]}
          labelFormatter={(l) => `Underlying $${l}`}
        />
        <ReferenceLine y={0} stroke="#5c6677" />
        {strikes.filter(inWin).map((k) => (
          <ReferenceLine key={`k${k}`} x={k} stroke="#8b93a7" strokeDasharray="3 4" />
        ))}
        {spot != null && inWin(spot) && (
          <ReferenceLine x={spot} stroke="#e6e9f2" strokeOpacity={0.35} />
        )}
        {breakevens.filter(inWin).map((be) => (
          <ReferenceLine key={`be${be}`} x={be} stroke="#60a5fa"
            label={{ value: `$${be}`, position: "top", fill: "#60a5fa", fontSize: 11 }} />
        ))}
        <Area type="monotone" dataKey="pnl" stroke="url(#pnlStroke)" strokeWidth={2}
          fill="url(#pnlFill)" isAnimationActive={animate} animationDuration={300} animationEasing="ease" />
        {deltaData && deltaData.length >= 2 && (
          <>
            <YAxis yAxisId="delta" orientation="right" hide />
            <Line yAxisId="delta" type="monotone" dataKey="delta" data={deltaData}
              stroke="var(--accent)" strokeWidth={1.5} dot={false}
              isAnimationActive={animate} animationDuration={300} />
          </>
        )}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
