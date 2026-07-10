// frontend/src/components/BuilderTab.jsx
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import PayoffChart from "./PayoffChart.jsx";
import ChainGrid from "./ChainGrid.jsx";
import ExpiryTabs from "./builder/ExpiryTabs.jsx";
import StrikeRuler from "./builder/StrikeRuler.jsx";
import StatsRow from "./builder/StatsRow.jsx";
import { computeStats } from "./builder/payoffStats.js";

const money = (n) => (n == null ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const emptyLeg = (expiry, strike) => ({ action: "BUY", right: "C", strike, expiry, qty: 1 });
const dteOf = (iso) => Math.max(0, Math.round((new Date(iso + "T00:00:00") - new Date().setHours(0, 0, 0, 0)) / 86400000));
const fmtDay = (d) => d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

export default function BuilderTab() {
  const [underlying, setUnderlying] = useState("QQQ");
  const [chain, setChain] = useState({ expirations: [], strikes: [] });
  const [legs, setLegs] = useState([]);
  const [netDebit, setNetDebit] = useState(null);
  const [iv, setIv] = useState(0.25);
  const [payoff, setPayoff] = useState(null);
  const [target, setTarget] = useState(10);
  const [stop, setStop] = useState(25);
  const [contracts, setContracts] = useState(1);
  const [trades, setTrades] = useState([]);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [selectedLeg, setSelectedLeg] = useState(null); // index into legs
  const [spot, setSpot] = useState(null);
  const [spotInfo, setSpotInfo] = useState(null);       // {price, change_abs, change_pct}
  const [rangePct, setRangePct] = useState(8);          // chart x-window ±%
  const [evalDays, setEvalDays] = useState(null);       // DATE scrubber: null = at expiration

  const loadChain = (sym) => {
    api.builderChain(sym).then((c) => { setChain(c); }).catch((e) => setMsg(e.message));
    api.builderSpot(sym).then((s) => {
      setSpotInfo(s);
      if (s?.price != null) setSpot(s.price);
    }).catch(() => {});
  };

  // Auto-load the chain: on mount and (debounced) whenever the underlying changes.
  // A symbol change also resets the legs — strikes/expiries from another chain are
  // meaningless, and a stale net_debit must never survive the switch.
  useEffect(() => {
    if (!underlying.trim()) return;
    const t = setTimeout(() => {
      setLegs([]); setSelectedLeg(null); setNetDebit(null); setPayoff(null);
      loadChain(underlying.trim());
    }, 500);
    return () => clearTimeout(t);
  }, [underlying]);
  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades)).catch(() => {});

  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);

  // Any change to the legs invalidates the fetched quote/payoff: net_debit is what the
  // monitor's target/stop uses, so it must never be saved against a different leg set.
  // Clearing it forces a re-fetch (Preview/Save are gated on netDebit != null).
  const invalidateQuote = () => { setNetDebit(null); setPayoff(null); };
  const setLeg = (i, patch) => { setLegs((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch, mid: null } : l))); invalidateQuote(); };

  // The expiry tabs + strike ruler edit the SELECTED leg. With no legs yet, first
  // interaction creates leg 0 (BUY CALL at the picked value) and selects it.
  const atmStrike = () => {
    const ks = chain.strikes;
    if (!ks.length) return 0;
    if (spot == null) return ks[Math.floor(ks.length / 2)];
    return ks.reduce((a, b) => (Math.abs(b - spot) < Math.abs(a - spot) ? b : a));
  };
  const pickExpiry = (iso) => {
    if (selectedLeg == null || !legs[selectedLeg]) {
      setLegs((ls) => [...ls, { ...emptyLeg(iso, atmStrike()) }]);
      setSelectedLeg(legs.length);
      invalidateQuote();
    } else setLeg(selectedLeg, { expiry: iso });
  };
  const pickStrike = (k) => {
    if (selectedLeg == null || !legs[selectedLeg]) {
      setLegs((ls) => [...ls, { ...emptyLeg(chain.expirations[0] || "", k) }]);
      setSelectedLeg(legs.length);
      invalidateQuote();
    } else setLeg(selectedLeg, { strike: k });
  };

  const addLeg = () => { setLegs((ls) => [...ls, emptyLeg(chain.expirations[0] || "", atmStrike())]); setSelectedLeg(legs.length); invalidateQuote(); };
  const removeLeg = (i) => {
    setLegs((ls) => ls.filter((_, j) => j !== i));
    setSelectedLeg((sel) => (sel == null ? null : sel === i ? (i > 0 ? i - 1 : null) : sel > i ? sel - 1 : sel));
    invalidateQuote();
  };
  const addLegFromChain = (leg) => { setLegs((ls) => [...ls, { ...leg, mid: null }]); setSelectedLeg(legs.length); invalidateQuote(); };

  const fetchQuotes = async () => {
    setBusy(true); setMsg(null);
    try {
      const q = await api.builderQuote(underlying, legs);
      setNetDebit(q.net_debit);
      setLegs((ls) => ls.map((l, i) => ({ ...l, mid: q.legs[i]?.mid })));
      if (q.missing?.length) setMsg(`No quote for ${q.missing.length} leg(s).`);
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const preview = async (days = evalDays) => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try {
      const body = { legs, net_debit: netDebit, iv: Number(iv) };
      if (spot != null) { body.lo = spot * 0.5; body.hi = spot * 1.5; body.points = 161; }
      // DATE scrubber: value BEFORE the nearest expiry re-prices via Black-Scholes at that
      // date; null/max = the backend default (at the nearest expiration).
      if (days != null && maxDays > 0 && days < maxDays) {
        const d = new Date(); d.setDate(d.getDate() + days);
        body.eval_date = d.toISOString().slice(0, 10);
      }
      setPayoff(await api.builderPayoff(body));
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const save = async () => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try {
      await api.builderSaveTrade({
        underlying, legs, net_debit: netDebit, iv: Number(iv),
        target_pct: target === "" ? null : Number(target) / 100,
        stop_pct: stop === "" ? null : Number(stop) / 100,
        contracts: Math.max(1, Number(contracts) || 1),
      });
      setMsg("Saved as a tracked paper trade.");
      loadTrades();
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const closeTrade = async (id) => { await api.builderCloseTrade(id).catch(() => {}); loadTrades(); };

  const debitLabel = useMemo(() => netDebit == null ? "—"
    : netDebit >= 0 ? `$${netDebit.toFixed(2)} debit` : `$${Math.abs(netDebit).toFixed(2)} credit`, [netDebit]);

  const sel = selectedLeg != null ? legs[selectedLeg] : null;
  // Nearest leg expiry bounds the DATE scrubber (BS is priced up to the front expiration).
  const nearestExpiry = useMemo(() => {
    const es = legs.map((l) => l.expiry).filter(Boolean).sort();
    return es[0] || null;
  }, [legs]);
  const maxDays = nearestExpiry ? dteOf(nearestExpiry) : 0;
  const dateLabel = useMemo(() => {
    const days = evalDays == null ? maxDays : Math.min(evalDays, maxDays);
    const d = new Date(); d.setDate(d.getDate() + days);
    return `${fmtDay(d)} (${days}d)`;
  }, [evalDays, maxDays]);
  const stats = useMemo(
    () => (payoff ? computeStats(payoff.curve, payoff.breakevens, spot, legs) : null),
    [payoff, spot, legs]);
  const chartWindow = useMemo(() => {
    if (!payoff) return null;
    if (spot == null) return null; // full curve
    return [spot * (1 - rangePct / 100), spot * (1 + rangePct / 100)];
  }, [payoff, spot, rangePct]);
  const legStrikes = useMemo(() => [...new Set(legs.map((l) => l.strike))], [legs]);

  return (
    <div className="builder-tab">
      <h1>Options Builder</h1>
      <p className="muted small">Build a multi-leg structure, preview its payoff, and save it as a
        tracked <b>paper</b> trade. Nothing is transmitted to the broker.</p>

      <section className="card">
        <div className="row gap center">
          <input className="input narrow" value={underlying}
            onChange={(e) => { setUnderlying(e.target.value.toUpperCase()); setSpot(null); setSpotInfo(null); }} placeholder="Underlying" />
          {spotInfo?.price != null && (
            <span className="hdr-quote">
              <b className="hdr-price">${spotInfo.price.toFixed(2)}</b>
              {spotInfo.change_pct != null && (
                <span className={`hdr-chg ${spotInfo.change_abs >= 0 ? "pos" : "neg"}`}>
                  <span>{spotInfo.change_abs >= 0 ? "+" : ""}{(spotInfo.change_pct * 100).toFixed(2)}%</span>
                  <span>{spotInfo.change_abs >= 0 ? "+" : "−"}${Math.abs(spotInfo.change_abs).toFixed(2)}</span>
                </span>
              )}
            </span>
          )}
          <span className="muted small">{chain.expirations.length} expiries · {chain.strikes.length} strikes</span>
        </div>

        {chain.expirations.length ? (
          <>
            <div className="olabel" style={{ marginTop: 10 }}>EXPIRATION: <b>{sel?.expiry ? `${dteOf(sel.expiry)}d` : "—"}</b></div>
            <ExpiryTabs expirations={chain.expirations} value={sel?.expiry || null} onChange={pickExpiry} />
            <div className="olabel">STRIKE:</div>
            <StrikeRuler strikes={chain.strikes} value={sel?.strike ?? null}
              right={sel?.right || "C"} action={sel?.action || "BUY"} spot={spot} onChange={pickStrike} />
          </>
        ) : (
          <div className="muted" style={{ marginTop: 10 }}>Load a chain to pick expiry and strike.</div>
        )}

        <StatsRow netDebit={netDebit} stats={stats} />

        {payoff ? (
          <PayoffChart curve={payoff.curve} breakevens={payoff.breakevens}
            strikes={legStrikes} spot={spot} window={chartWindow} />
        ) : (
          <div className="muted">Fetch quotes, then Preview to see the payoff.</div>
        )}

        {maxDays > 0 && (
          <div className="row gap center date-row">
            <label className="olabel">DATE: <b>{dateLabel}</b></label>
            <input type="range" min="0" max={maxDays} step="1"
              value={evalDays == null ? maxDays : Math.min(evalDays, maxDays)}
              onChange={(e) => setEvalDays(Number(e.target.value))}
              onPointerUp={() => { if (netDebit != null) preview(); }} />
            <span className="muted small">(At expiration)</span>
          </div>
        )}

        <div className="row gap center sliders-row">
          <label className="olabel">RANGE: <b>±{rangePct}%</b></label>
          <input type="range" min="2" max="50" step="1" value={rangePct}
            onChange={(e) => setRangePct(Number(e.target.value))} />
          <label className="olabel">🌡 IMPLIED VOLATILITY: <b>{(Number(iv) * 100).toFixed(1)}%</b></label>
          <input type="range" min="0.05" max="1.5" step="0.01" value={iv}
            onChange={(e) => setIv(e.target.value)}
            onPointerUp={() => { if (netDebit != null) preview(); }} />
          <button className="btn" onClick={() => preview()} disabled={busy || !legs.length}>Preview</button>
        </div>

        <h4>Legs</h4>
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Side</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Mid</th><th></th></tr></thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i} className={i === selectedLeg ? "leg-selected" : ""}
                  onClick={() => setSelectedLeg(i)}>
                  <td><select value={l.action} onChange={(e) => setLeg(i, { action: e.target.value })}><option>BUY</option><option>SELL</option></select></td>
                  <td><select value={l.right} onChange={(e) => setLeg(i, { right: e.target.value })}><option value="C">Call</option><option value="P">Put</option></select></td>
                  <td><select value={l.strike} onChange={(e) => setLeg(i, { strike: Number(e.target.value) })}>{chain.strikes.map((s) => <option key={s} value={s}>{s}</option>)}</select></td>
                  <td><select value={l.expiry} onChange={(e) => setLeg(i, { expiry: e.target.value })}>{chain.expirations.map((x) => <option key={x} value={x}>{x}</option>)}</select></td>
                  <td><input className="input tiny" type="number" min="1" value={l.qty} onChange={(e) => setLeg(i, { qty: Number(e.target.value) })} /></td>
                  <td className="num">{l.mid == null ? "—" : l.mid.toFixed(2)}</td>
                  <td><button className="btn ghost tiny" onClick={(e) => { e.stopPropagation(); removeLeg(i); }}>✕</button></td>
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

      <ChainGrid key={underlying} underlying={underlying} expirations={chain.expirations}
        onAddLeg={addLegFromChain} onSpot={setSpot} />

      <section className="card">
        <h2>Save as tracked trade</h2>
        <div className="row gap center">
          <label className="muted small">Target % of debit</label>
          <input className="input tiny" type="number" value={target} onChange={(e) => setTarget(e.target.value)} />
          <label className="muted small">Stop %</label>
          <input className="input tiny" type="number" value={stop} onChange={(e) => setStop(e.target.value)} />
          <label className="muted small">Contracts</label>
          <input className="input tiny" type="number" min="1" value={contracts} onChange={(e) => setContracts(e.target.value)} />
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
