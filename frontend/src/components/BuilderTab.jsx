// frontend/src/components/BuilderTab.jsx
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import PayoffChart from "./PayoffChart.jsx";
import ChainGrid from "./ChainGrid.jsx";
import ExpiryTabs from "./builder/ExpiryTabs.jsx";
import StrikeRuler from "./builder/StrikeRuler.jsx";
import StatCards from "./builder/StatCards.jsx";
import { computeStats } from "./builder/payoffStats.js";
import { buildTemplate, buildHedge } from "./builder/templates.js";

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
  const [panPct, setPanPct] = useState(0);              // chart x-window pan offset (% of spot)
  const [evalDays, setEvalDays] = useState(null);       // DATE scrubber: null = at expiration
  const [isTheo, setIsTheo] = useState(false);           // net debit built from BS theo values
  const [expiryStrikes, setExpiryStrikes] = useState({}); // iso -> strikes for that expiry
  const [mkt, setMkt] = useState(null);

  const underlyingRef = useRef(underlying);
  useEffect(() => { underlyingRef.current = underlying; }, [underlying]);

  const dragRef = useRef(null);          // { x, pan } while a pointer-drag pan is active
  const chartZoneRef = useRef(null);     // wrapper div — carries a non-passive wheel listener
  const clampPan = (p) => Math.min(50 - rangePct, Math.max(-(50 - rangePct), p));

  // Spot can miss right after a server restart (cold data stream) — retry a few
  // times, but never apply a late result for a symbol the user has moved on from.
  const loadSpot = (sym, attempt = 0) => {
    api.builderSpot(sym).then((s) => {
      if (underlyingRef.current !== sym) return;
      setSpotInfo(s);
      if (s?.price != null) setSpot(s.price);
      else if (attempt < 3) setTimeout(() => loadSpot(sym, attempt + 1), 4000);
    }).catch(() => {
      if (underlyingRef.current === sym && attempt < 3) setTimeout(() => loadSpot(sym, attempt + 1), 4000);
    });
  };
  const loadChain = (sym) => {
    api.builderChain(sym).then((c) => { setChain(c); }).catch((e) => setMsg(e.message));
    loadSpot(sym);
  };

  // Auto-load the chain: on mount and (debounced) whenever the underlying changes.
  // A symbol change also resets the legs — strikes/expiries from another chain are
  // meaningless, and a stale net_debit must never survive the switch.
  useEffect(() => {
    if (!underlying.trim()) return;
    const t = setTimeout(() => {
      setLegs([]); setSelectedLeg(null); setNetDebit(null); setPayoff(null);
      setExpiryStrikes({});
      loadChain(underlying.trim());
    }, 500);
    return () => clearTimeout(t);
  }, [underlying]);
  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades)).catch(() => {});

  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);

  const loadMarketStatus = () => api.marketStatus().then(setMkt).catch(() => {});
  useEffect(() => { loadMarketStatus(); }, []);
  useEffect(() => { const id = setInterval(loadMarketStatus, 30000); return () => clearInterval(id); }, []);

  // Any change to the legs invalidates the fetched quote/payoff: net_debit is what the
  // monitor's target/stop uses, so it must never be saved against a different leg set.
  // Clearing it forces a re-fetch (Preview/Save are gated on netDebit != null).
  const invalidateQuote = () => { setNetDebit(null); setPayoff(null); setIsTheo(false); setPanPct(0); };
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

  // Templates menu: first click on a template arms it ("Replace legs with X?"), a
  // second click within 5s applies it. Any other click re-arms (or arms a different one).
  const [tmplOpen, setTmplOpen] = useState(false);
  const [armedTmpl, setArmedTmpl] = useState(null);
  const armedAt = useRef(0);
  const applyTemplate = (name) => {
    const now = Date.now();
    if (armedTmpl !== name || now - armedAt.current > 5000) {
      setArmedTmpl(name); armedAt.current = now; return;      // first click arms
    }
    const expiry = sel?.expiry || chain.expirations[0];
    const built = buildTemplate(name, { expiry, strikes: strikesFor(expiry), spot });
    if (built) { setLegs(built); setSelectedLeg(0); invalidateQuote(); }
    setArmedTmpl(null); setTmplOpen(false);
  };
  const applyHedge = () => {
    const h = buildHedge(legs, { spot, strikesFor });
    if (!h) { setMsg("No hedge suggested (flat bias or no option expiry)."); return; }
    setLegs((ls) => [...ls, h]); setSelectedLeg(legs.length); invalidateQuote();
  };
  const duplicateLeg = (i) => {
    setLegs((ls) => [...ls.slice(0, i + 1), { ...ls[i], mid: null }, ...ls.slice(i + 1)]);
    setSelectedLeg(i + 1); invalidateQuote();
  };

  const fetchQuotes = async () => {
    setBusy(true); setMsg(null);
    try {
      const q = await api.builderQuote(underlying, legs, spot, iv);
      setNetDebit(q.net_debit);
      setIsTheo(!!q.theoretical);
      setLegs((ls) => ls.map((l, i) => ({ ...l, mid: q.legs[i]?.mid, theo: q.legs[i]?.theo })));
      if (q.missing?.length) setMsg(`No quote for ${q.missing.length} leg(s).`);
      else if (q.theoretical) setMsg("No live quotes right now — using Black-Scholes theoretical values (saving disabled).");
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };

  const preview = async (days = evalDays) => {
    if (netDebit == null) { setMsg("Fetch quotes first."); return; }
    setBusy(true); setMsg(null);
    try {
      const body = { legs, net_debit: netDebit, iv: Number(iv) };
      if (spot != null) { body.lo = spot * 0.5; body.hi = spot * 1.5; body.points = 161; body.spot = spot; }
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

  // Strikes for one expiry (fallback: the whole-chain union while loading).
  const strikesFor = (iso) => (iso && expiryStrikes[iso]) || chain.strikes;
  useEffect(() => {
    const iso = legs[selectedLeg!= null ? selectedLeg : -1]?.expiry;
    if (!iso || expiryStrikes[iso]) return;
    let stale = false;
    api.builderChainStrikes(underlying, iso).then((d) => {
      if (!stale && d.strikes?.length) setExpiryStrikes((m) => ({ ...m, [iso]: d.strikes }));
    }).catch(() => {});
    return () => { stale = true; };
  }, [selectedLeg, legs, underlying]);
  // After an expiry switch, snap the leg's strike to the nearest strike that actually
  // exists for that expiry (per-expiry lists load async).
  useEffect(() => {
    if (selectedLeg == null) return;
    const l = legs[selectedLeg];
    const ks = l && expiryStrikes[l.expiry];
    if (!l || !ks?.length || ks.includes(l.strike)) return;
    const nearest = ks.reduce((a, b) => (Math.abs(b - l.strike) < Math.abs(a - l.strike) ? b : a));
    setLeg(selectedLeg, { strike: nearest });
  }, [expiryStrikes, selectedLeg]);

  // Fully automatic flow: legs settle (debounced) -> quotes fetch -> payoff previews.
  // Keyed on the legs' CONTENTS (mid/theo excluded): fetchQuotes rewrites `legs` to
  // attach mids, so keying on the array identity would loop forever.
  const legsKey = useMemo(
    () => JSON.stringify(legs.map((l) => [l.action, l.right, l.strike, l.expiry, l.qty])),
    [legs]);
  useEffect(() => {
    if (!legs.length || legs.some((l) => l.right !== "S" && (!l.expiry || !l.strike))) return;
    const t = setTimeout(() => { fetchQuotes(); }, 600);
    return () => clearTimeout(t);
  }, [legsKey]);
  // Self-healing retry: legs present but still no net debit (transient fetch failure /
  // cold stream) -> try again every 5s until a quote (live or theo) lands.
  useEffect(() => {
    if (!legs.length || netDebit != null) return;
    const id = setInterval(() => { if (!busy) fetchQuotes(); }, 5000);
    return () => clearInterval(id);
  }, [legsKey, netDebit, busy]);
  useEffect(() => {
    if (netDebit != null && legs.length) preview();
  }, [netDebit]);

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
    const off = spot * (clampPan(panPct) / 100);
    return [spot * (1 - rangePct / 100) + off, spot * (1 + rangePct / 100) + off];
  }, [payoff, spot, rangePct, panPct]);
  const legStrikes = useMemo(() => [...new Set(legs.map((l) => l.strike))], [legs]);

  // React's synthetic onWheel is passive, so e.preventDefault() there throws and the
  // page scrolls anyway. Attach a real, non-passive DOM listener instead.
  useEffect(() => {
    const el = chartZoneRef.current;
    if (!el) return undefined;
    const onWheel = (e) => {
      if (!payoff || spot == null) return;
      e.preventDefault();
      setRangePct((r) => Math.min(50, Math.max(2, Math.round(r * (e.deltaY > 0 ? 1.15 : 0.87)))));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [payoff, spot]);

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
          <span className={`mkt-chip ${mkt?.is_open ? "open" : "closed"}`}>
            <span className="mkt-dot" />
            {mkt?.is_open ? "Market Open" : (mkt?.session === "closed" ? "Closed" : mkt?.session?.replace("_", "-"))}
          </span>
          <span className="muted small">{chain.expirations.length} expiries · {chain.strikes.length} strikes</span>
        </div>

        {chain.expirations.length ? (
          <>
            <div className="olabel" style={{ marginTop: 10 }}>EXPIRATION: <b>{sel?.expiry ? `${dteOf(sel.expiry)}d` : "—"}</b></div>
            <ExpiryTabs expirations={chain.expirations} value={sel?.expiry || null} onChange={pickExpiry} />
            <div className="olabel">STRIKE:</div>
            <StrikeRuler strikes={strikesFor(sel?.expiry)} value={sel?.strike ?? null}
              right={sel?.right || "C"} action={sel?.action || "BUY"} spot={spot}
              legs={legs} selected={selectedLeg} onSelect={setSelectedLeg}
              onChange={pickStrike} />
          </>
        ) : (
          <div className="muted" style={{ marginTop: 10 }}>Load a chain to pick expiry and strike.</div>
        )}

        <StatCards netDebit={netDebit} stats={stats} chance={payoff?.chance_of_profit} />

        {payoff ? (
          <div className="chart-zone" ref={chartZoneRef}
            onPointerDown={(e) => { dragRef.current = { x: e.clientX, pan: panPct, dragging: false }; }}
            onPointerMove={(e) => {
              if (!dragRef.current || spot == null) return;
              const dx = e.clientX - dragRef.current.x;
              if (!dragRef.current.dragging && Math.abs(dx) <= 4) return;
              dragRef.current.dragging = true;
              const dxPct = (dx / e.currentTarget.clientWidth) * rangePct * 2;
              setPanPct(clampPan(dragRef.current.pan - dxPct));
            }}
            onPointerUp={() => { dragRef.current = null; }}
            onPointerLeave={() => { dragRef.current = null; }}
            onDoubleClick={() => { setRangePct(8); setPanPct(0); }}
            title="Scroll to zoom · drag to pan · double-click to reset">
            <PayoffChart curve={payoff.curve} breakevens={payoff.breakevens}
              strikes={legStrikes} spot={spot} window={chartWindow} />
          </div>
        ) : (
          <div className="muted">
            {legs.length ? (busy ? "Fetching quotes…" : "Waiting for a quote — the payoff renders automatically.")
              : "Pick an expiry and strike above — quotes and the payoff load automatically."}
          </div>
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
          <button className="btn ghost" disabled title="Greeks overlay — coming in Phase 2">Δ overlay</button>
        </div>

        <div className="legs-toolbar">
          <h4 style={{ margin: 0 }}>LEGS</h4>
          <div className="row gap center">
            <button className="btn ghost" onClick={addLeg} disabled={!chain.expirations.length}>+ Leg</button>
            <div className="tmpl-wrap">
              <button className="btn ghost" onClick={() => setTmplOpen((o) => !o)} disabled={!chain.expirations.length || spot == null}>Templates ▾</button>
              {tmplOpen && (
                <div className="tmpl-menu" onMouseLeave={() => { setArmedTmpl(null); }}>
                  {[["vertical", "Vertical Spread"], ["straddle", "Straddle"], ["butterfly", "Butterfly"],
                    ["iron_condor", "Iron Condor"], ["covered_call", "Covered Call"]].map(([k, label]) => (
                    <button key={k} className={`tmpl-item ${armedTmpl === k ? "armed" : ""}`}
                      onClick={() => applyTemplate(k)}>
                      {armedTmpl === k ? `Replace legs with ${label}?` : label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button className="btn ghost" onClick={applyHedge} disabled={!legs.length || spot == null}>Add hedge</button>
            <span className="muted">Net: <b className="num">{isTheo ? "~" : ""}{debitLabel}</b></span>
          </div>
        </div>
        {tmplOpen && <div className="tmpl-backdrop" onClick={() => { setTmplOpen(false); setArmedTmpl(null); }} />}
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Side</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Mid</th><th></th></tr></thead>
            <tbody>
              {legs.map((l, i) => {
                const isStock = l.right === "S";
                const onTypeChange = (newRight) => {
                  if (newRight === "S" && !isStock) setLeg(i, { right: "S", strike: 0, expiry: "", qty: 100 });
                  else if (newRight !== "S" && isStock) setLeg(i, { right: newRight, strike: atmStrike(), expiry: chain.expirations[0] || "" });
                  else setLeg(i, { right: newRight });
                };
                return (
                  <tr key={i} className={i === selectedLeg ? "leg-selected" : ""}
                    onClick={() => setSelectedLeg(i)}>
                    <td>
                      <button className={`side-badge ${l.action === "BUY" ? "buy" : "sell"}`}
                        onClick={(e) => { e.stopPropagation(); setLeg(i, { action: l.action === "BUY" ? "SELL" : "BUY" }); }}>
                        {l.action}
                      </button>
                    </td>
                    <td className={`right-${l.right}`}>
                      <select value={l.right} onChange={(e) => onTypeChange(e.target.value)}>
                        <option value="C">Call</option><option value="P">Put</option><option value="S">Stock</option>
                      </select>
                    </td>
                    <td>
                      {isStock ? <span className="muted">—</span> : (
                        <select value={l.strike} onChange={(e) => setLeg(i, { strike: Number(e.target.value) })}>
                          {(strikesFor(l.expiry)).map((s) => <option key={s} value={s}>{s}</option>)}
                          {!strikesFor(l.expiry).includes(l.strike) && <option value={l.strike}>{l.strike}</option>}
                        </select>
                      )}
                    </td>
                    <td>
                      {isStock ? <span className="muted">—</span> : (
                        <select value={l.expiry} onChange={(e) => setLeg(i, { expiry: e.target.value })}>
                          {chain.expirations.map((x) => <option key={x} value={x}>{x}</option>)}
                        </select>
                      )}
                    </td>
                    <td><input className="input tiny" type="number" min="1" value={l.qty} onChange={(e) => setLeg(i, { qty: Number(e.target.value) })} /></td>
                    <td className="num">{l.mid == null ? "—" : `${l.theo ? "~" : ""}${l.mid.toFixed(2)}`}</td>
                    <td>
                      <div className="row gap">
                        <button className="btn ghost tiny" title="Duplicate" onClick={(e) => { e.stopPropagation(); duplicateLeg(i); }}>⧉</button>
                        <button className="btn ghost tiny" title="Delete" onClick={(e) => { e.stopPropagation(); removeLeg(i); }}>✕</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
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
          <button className="btn primary" onClick={save} disabled={busy || netDebit == null || isTheo}>Save trade</button>
          {isTheo && <span className="muted small">Theoretical prices — saving needs live quotes.</span>}
        </div>
        {msg && <div className="banner ok">{msg}</div>}
      </section>

      <section className="card">
        <h2>Recent Trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="trade-cards">
            {trades.map((t) => {
              const v = t.status === "closed" ? t.pnl : t.last_pnl;
              return (
                <div className="trade-card" key={t.id}>
                  <div className="trade-head">
                    <b>{t.underlying}</b>
                    <span className="muted small">{t.legs.length} legs · {t.entry_time?.slice(0, 10) || "—"}</span>
                  </div>
                  <div className={`trade-pnl num ${(v ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {v == null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`}
                    {t.last_pnl_pct != null && t.status === "open" &&
                      <span className="muted small"> {(t.last_pnl_pct * 100).toFixed(1)}%</span>}
                  </div>
                  <div className="row between center">
                    <span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>{t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span>
                    {t.status === "open" && <button className="btn ghost tiny" onClick={() => closeTrade(t.id)}>Close</button>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
