// frontend/src/components/ChainGrid.jsx
import { useState } from "react";
import { api } from "../api.js";

// Visible option chain: all expiries as DTE chips + a near-ATM call/strike/put grid for the
// selected expiry. Tapping a price calls onAddLeg to append a BUY leg (side edited upstream).

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const label = (iso) => { const [y, m, d] = iso.split("-"); return `${MON[+m - 1]}-${d}`; };
const dte = (iso) => Math.max(0, Math.round((new Date(iso + "T00:00:00") - new Date().setHours(0, 0, 0, 0)) / 86400000));
const mid = (n) => (n == null ? "—" : n.toFixed(2));

export default function ChainGrid({ underlying, expirations = [], onAddLeg, onSpot }) {
  const [expiry, setExpiry] = useState(null);
  const [count, setCount] = useState(8);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = async (exp, cnt) => {
    setLoading(true); setErr(null); setExpiry(exp);
    try {
      const d = await api.builderChainQuotes(underlying, exp, cnt);
      setData(d);
      if (d?.spot > 0) onSpot?.(d.spot);
    }
    catch (e) { setErr(e.message); setData(null); }
    finally { setLoading(false); }
  };

  if (!expirations.length) return null;

  return (
    <section className="card chain-card">
      <div className="row between center">
        <h2>Option chain — {underlying}{data?.spot > 0 && <span className="badge">spot {data.spot.toFixed(2)}</span>}</h2>
        <div className="row gap center">
          <span className="muted small">Strikes ±</span>
          <select className="input tiny" value={count}
            onChange={(e) => { const c = Number(e.target.value); setCount(c); if (expiry) load(expiry, c); }}>
            {[6, 8, 12, 20].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          {expiry && <button className="btn ghost tiny" onClick={() => load(expiry, count)}>↻ Refresh</button>}
        </div>
      </div>

      <div className="muted small" style={{ marginTop: 10 }}>All expiries ({expirations.length}) — tap one to load quotes</div>
      <div className="chips">
        {expirations.map((x) => (
          <span key={x} className={`chip ${x === expiry ? "active" : ""}`} onClick={() => load(x, count)}>
            {label(x)} <span className="dte">{dte(x)}d</span>
          </span>
        ))}
      </div>

      {loading && <div className="muted">Loading quotes…</div>}
      {err && <div className="banner error">{err}</div>}
      {!loading && data && data.rows.length === 0 && expiry && (
        <div className="muted">No quotes (market closed or unavailable).</div>
      )}
      {!loading && data && data.rows.length > 0 && (
        <div className="table-scroll">
          <table className="table chain-grid">
            <thead><tr><th className="callh">Call mid</th><th className="strikeh">Strike</th><th className="puth">Put mid</th></tr></thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.strike} className={r.strike === data.atm ? "atm" : ""}>
                  <td className={`price call ${r.call_mid == null ? "muted" : ""}`}
                    onClick={() => r.call_mid != null && onAddLeg({ action: "BUY", right: "C", strike: r.strike, expiry: data.expiry, qty: 1 })}>
                    {mid(r.call_mid)}
                  </td>
                  <td className="strike num">{r.strike}</td>
                  <td className={`price put ${r.put_mid == null ? "muted" : ""}`}
                    onClick={() => r.put_mid != null && onAddLeg({ action: "BUY", right: "P", strike: r.strike, expiry: data.expiry, qty: 1 })}>
                    {mid(r.put_mid)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="hint muted small">Tap a call or put price to add it as a leg. “—” = no quote.</div>
        </div>
      )}
    </section>
  );
}
