import { useEffect, useState } from "react";
import { ResponsiveContainer, Scatter, ScatterChart, XAxis, YAxis } from "recharts";
import { api } from "../../api.js";
import { Dialog } from "./controls/Overlay.jsx";

const clock = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hourCycle: "h23", hour: "2-digit", minute: "2-digit",
});
const price = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const shares = new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 });

function sessionAt(time, data) {
  if (data.regular_open_ms == null) return "Extended";
  if (time < data.regular_open_ms) return "Pre-market";
  return time < data.regular_close_ms ? "Regular" : "After hours";
}

export default function SavedMinutesDialog({ symbol, onClose }) {
  const [selection, setSelection] = useState({ day: "" });
  const [result, setResult] = useState(null);
  useEffect(() => {
    let current = true;
    api.savedMinutes(symbol, selection.day)
      .then(data => { if (current) setResult({ selection, data }); })
      .catch(() => { if (current) setResult({ selection, error: "Could not read saved minutes." }); });
    return () => { current = false; };
  }, [symbol, selection]);

  const loaded = result?.selection === selection;
  const data = loaded && result.data;
  const bars = data?.bars || [];
  return <Dialog open onClose={onClose} title={`Saved 1m · ${symbol}`} width={760}>
    <div className="cx-minutes-controls">
      <label className="cx-field">Session date (ET)
        <input type="date" data-autofocus value={selection.day || data?.day || ""}
          onChange={e => setSelection({ day: e.target.value })}/>
      </label>
      <button type="button" onClick={() => setSelection({ day: "" })}>Latest saved</button>
    </div>
    <p className="cx-muted" role="status">{!loaded ? "Loading saved minutes…"
      : result.error || (bars.length
        ? `${bars.length} saved minutes · ${clock.format(bars[0].time)}–${clock.format(bars.at(-1).time)} ET`
        : `No saved minutes for ${symbol}${data?.day ? " on " + data.day : ""}.`)}</p>
    {bars.length > 0 && <>
      <div aria-label={`${symbol} saved minute closing prices in USD`} role="img">
        <ResponsiveContainer width="100%" height={140}>
          <ScatterChart margin={{ left: 0, right: 12, top: 8, bottom: 0 }}>
            <XAxis type="number" dataKey="time" domain={["dataMin", "dataMax"]} minTickGap={40}
              tickFormatter={time => clock.format(time)} tick={{ fontSize: 11 }} />
            <YAxis type="number" dataKey="close" domain={["auto", "auto"]} width={64}
              tickFormatter={value => price.format(value)} tick={{ fontSize: 11 }} />
            <Scatter data={bars} isAnimationActive={false}
              shape={({ cx, cy }) => <circle cx={cx} cy={cy} r={1.7} fill="var(--cx-accent)"/>}/>
          </ScatterChart>
        </ResponsiveContainer>
      </div>
      <div className="cx-minutes-scroll" role="region" aria-label="Saved minute bars" tabIndex={0}>
        <table aria-label={`${symbol} one-minute bars, newest first`}>
          <thead><tr>{["Time (ET)", "Session", "Open", "High", "Low", "Close", "Volume"].map(name =>
            <th scope="col" key={name}>{name}</th>)}</tr></thead>
          <tbody>{bars.slice().reverse().map(bar => <tr key={bar.time}>
            <th scope="row">{clock.format(bar.time)}</th><td>{sessionAt(bar.time, data)}</td>
            {[bar.open, bar.high, bar.low, bar.close].map((value, i) => <td key={i}>{price.format(value)}</td>)}
            <td>{shares.format(bar.volume)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
  </Dialog>;
}
