// frontend/src/components/BuilderTab.jsx
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import PayoffChart from "./PayoffChart.jsx";
import ChainGrid from "./ChainGrid.jsx";

const money = (n) => (n == null ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const emptyLeg = (expiry, strike) => ({ action: "BUY", right: "C", strike, expiry, qty: 1 });

export default function BuilderTab() {
  const [underlying, setUnderlying] = useState("QQQ");
  const [chain, setChain] = useState({ expirations: [], strikes: [] });
  const [legs, setLegs] = useState([]);
  const [netDebit, setNetDebit] = useState(null);
  const [iv, setIv] = useState(0.25);
  const [payoff, setPayoff] = useState(null);
  const [target, setTarget] = useState(10);
  const [stop, setStop] = useState(25);
  const [trades, setTrades] = useState([]);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const loadChain = () => api.builderChain(underlying)
    .then((c) => { setChain(c); }).catch((e) => setMsg(e.message));
  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades)).catch(() => {});

  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);

  // Any change to the legs invalidates the fetched quote/payoff: net_debit is what the
  // monitor's target/stop uses, so it must never be saved against a different leg set.
  // Clearing it forces a re-fetch (Preview/Save are gated on netDebit != null).
  const invalidateQuote = () => { setNetDebit(null); setPayoff(null); };
  const addLeg = () => { setLegs((ls) => [...ls, emptyLeg(chain.expirations[0] || "", chain.strikes[Math.floor(chain.strikes.length / 2)] || 0)]); invalidateQuote(); };
  const setLeg = (i, patch) => { setLegs((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch, mid: null } : l))); invalidateQuote(); };
  const removeLeg = (i) => { setLegs((ls) => ls.filter((_, j) => j !== i)); invalidateQuote(); };
  const addLegFromChain = (leg) => { setLegs((ls) => [...ls, { ...leg, mid: null }]); invalidateQuote(); };

  const fetchQuotes = async () => {
    setBusy(true); setMsg(null);
    try {
      const q = await api.builderQuote(underlying, legs);
      setNetDebit(q.net_debit);
      setLegs((ls) => ls.map((l, i) => ({ ...l, mid: q.legs[i]?.mid })));
      if (q.missing?.length) setMsg(`No quote for ${q.missing.length} leg(s).`);
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const preview = async () => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try { setPayoff(await api.builderPayoff({ legs, net_debit: netDebit, iv: Number(iv) })); }
    catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const save = async () => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try {
      await api.builderSaveTrade({
        underlying, legs, net_debit: netDebit, iv: Number(iv),
        target_pct: target === "" ? null : Number(target) / 100,
        stop_pct: stop === "" ? null : Number(stop) / 100,
      });
      setMsg("Saved as a tracked paper trade.");
      loadTrades();
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const closeTrade = async (id) => { await api.builderCloseTrade(id).catch(() => {}); loadTrades(); };

  const debitLabel = useMemo(() => netDebit == null ? "—"
    : netDebit >= 0 ? `$${netDebit.toFixed(2)} debit` : `$${Math.abs(netDebit).toFixed(2)} credit`, [netDebit]);

  return (
    <div className="builder-tab">
      <h1>Options Builder</h1>
      <p className="muted small">Build a multi-leg structure, preview its payoff, and save it as a
        tracked <b>paper</b> trade. Nothing is transmitted to the broker.</p>

      <section className="card">
        <div className="row gap center">
          <input className="input narrow" value={underlying}
            onChange={(e) => setUnderlying(e.target.value.toUpperCase())} placeholder="Underlying" />
          <button className="btn" onClick={loadChain}>Load chain</button>
          <span className="muted small">{chain.expirations.length} expiries · {chain.strikes.length} strikes</span>
        </div>

        <h4>Legs</h4>
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Side</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Mid</th><th></th></tr></thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i}>
                  <td><select value={l.action} onChange={(e) => setLeg(i, { action: e.target.value })}><option>BUY</option><option>SELL</option></select></td>
                  <td><select value={l.right} onChange={(e) => setLeg(i, { right: e.target.value })}><option value="C">Call</option><option value="P">Put</option></select></td>
                  <td><select value={l.strike} onChange={(e) => setLeg(i, { strike: Number(e.target.value) })}>{chain.strikes.map((s) => <option key={s} value={s}>{s}</option>)}</select></td>
                  <td><select value={l.expiry} onChange={(e) => setLeg(i, { expiry: e.target.value })}>{chain.expirations.map((x) => <option key={x} value={x}>{x}</option>)}</select></td>
                  <td><input className="input tiny" type="number" min="1" value={l.qty} onChange={(e) => setLeg(i, { qty: Number(e.target.value) })} /></td>
                  <td className="num">{l.mid == null ? "—" : l.mid.toFixed(2)}</td>
                  <td><button className="btn ghost tiny" onClick={() => removeLeg(i)}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="row gap center">
          <button className="btn ghost" onClick={addLeg} disabled={!chain.expirations.length}>+ Add leg</button>
          <button className="btn" onClick={fetchQuotes} disabled={busy || !legs.length}>Fetch quotes</button>
          <span className="muted">Net: <b>{debitLabel}</b></span>
        </div>
      </section>

      <ChainGrid key={underlying} underlying={underlying} expirations={chain.expirations} onAddLeg={addLegFromChain} />

      <section className="card">
        <div className="row between center">
          <h2>Payoff</h2>
          <div className="row gap center">
            <label className="muted small">IV</label>
            <input className="input tiny" type="number" step="0.01" value={iv} onChange={(e) => setIv(e.target.value)} />
            <button className="btn" onClick={preview} disabled={busy || !legs.length}>Preview</button>
          </div>
        </div>
        {payoff ? (
          <>
            <PayoffChart curve={payoff.curve} breakevens={payoff.breakevens} />
            <div className="muted small">At {payoff.eval_date} · max profit {money(payoff.max_profit)} · max loss {money(payoff.max_loss)} · breakevens {payoff.breakevens.join(", ") || "—"}</div>
          </>
        ) : <div className="muted">Fetch quotes, then Preview to see the payoff.</div>}
      </section>

      <section className="card">
        <h2>Save as tracked trade</h2>
        <div className="row gap center">
          <label className="muted small">Target % of debit</label>
          <input className="input tiny" type="number" value={target} onChange={(e) => setTarget(e.target.value)} />
          <label className="muted small">Stop %</label>
          <input className="input tiny" type="number" value={stop} onChange={(e) => setStop(e.target.value)} />
          <button className="btn primary" onClick={save} disabled={busy || netDebit == null}>Save trade</button>
        </div>
        {msg && <div className="banner ok">{msg}</div>}
      </section>

      <section className="card">
        <h2>Tracked trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Underlying</th><th>Legs</th><th>Net debit</th><th>P&L</th><th>Status</th><th></th></tr></thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id}>
                    <td>{t.underlying}</td>
                    <td className="muted small">{t.legs.length} legs</td>
                    <td className="num">{t.net_debit >= 0 ? `$${t.net_debit.toFixed(2)}` : `($${Math.abs(t.net_debit).toFixed(2)})`}</td>
                    <td className={`num ${(t.status === "closed" ? t.pnl : t.last_pnl) >= 0 ? "pos" : "neg"}`}>
                      {money(t.status === "closed" ? t.pnl : t.last_pnl)}
                      {t.last_pnl_pct != null && t.status === "open" && <span className="muted small"> {(t.last_pnl_pct * 100).toFixed(1)}%</span>}
                    </td>
                    <td><span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>{t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span></td>
                    <td>{t.status === "open" && <button className="btn ghost tiny" onClick={() => closeTrade(t.id)}>Close</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
