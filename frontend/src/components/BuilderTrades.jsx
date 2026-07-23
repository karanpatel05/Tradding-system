// frontend/src/components/BuilderTrades.jsx
import { useEffect, useState } from "react";
import { api } from "../api.js";
import PayoffChart from "./PayoffChart.jsx";
import GreeksPanel from "./builder/GreeksPanel.jsx";
import { summarizeTrades } from "./builder/tradesSummary.js";

const money = (n) => (n == null ? "—" :
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pctv = (n) => (n == null ? "—" : `${(n * 100).toFixed(1)}%`);

// Trades tab: portfolio P&L summary + saved-trade list; clicking a trade expands its
// payoff diagram + net greeks. All read-only; paper trades only.
export default function BuilderTrades() {
  const [trades, setTrades] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);      // {full, spot, greeks} for selectedId
  const [detailErr, setDetailErr] = useState(false);

  const loadTrades = () => api.builderTrades().then((d) => setTrades(d.trades || [])).catch(() => {});
  useEffect(() => { loadTrades(); }, []);
  useEffect(() => { const id = setInterval(loadTrades, 15000); return () => clearInterval(id); }, []);

  // Fetch payoff + live spot + net greeks for the selected trade (immutable per trade,
  // so keyed on selectedId only; live P&L in the row re-renders from the poll).
  useEffect(() => {
    if (selectedId == null) { setDetail(null); setDetailErr(false); return; }
    const t = trades.find((x) => x.id === selectedId);
    if (!t) return;
    let cancelled = false;
    setDetail(null); setDetailErr(false);
    api.builderTrade(selectedId).then(async (full) => {
      const spotResp = await api.builderSpot(t.underlying).catch(() => null);
      const spot = spotResp?.price ?? null;
      const greeks = spot != null
        ? await api.builderGreeks({ legs: t.legs, spot, iv: t.iv }).catch(() => null)
        : null;
      if (!cancelled) setDetail({ full, spot, greeks });
    }).catch(() => { if (!cancelled) setDetailErr(true); });
    return () => { cancelled = true; };
  }, [selectedId]);  // eslint-disable-line react-hooks/exhaustive-deps

  const closeTrade = async (id, e) => {
    e.stopPropagation();
    await api.builderCloseTrade(id).catch(() => {});
    loadTrades();
  };

  const s = summarizeTrades(trades);

  return (
    <div className="builder-trades">
      <section className="card">
        <h2>Portfolio P&amp;L</h2>
        <div className="deploy-grid">
          <div><div className="stat-label">Net P&amp;L</div>
            <div className={`stat-value ${s.net >= 0 ? "pos" : "neg"}`}>{money(s.net)}</div></div>
          <div><div className="stat-label">Realized</div>
            <div className={`stat-value ${s.realized >= 0 ? "pos" : "neg"}`}>{money(s.realized)}</div></div>
          <div><div className="stat-label">Unrealized</div>
            <div className={`stat-value ${s.unrealized >= 0 ? "pos" : "neg"}`}>{money(s.unrealized)}</div></div>
          <div><div className="stat-label">Open / Closed</div>
            <div className="stat-value">{s.openCount} / {s.closedCount}</div></div>
          <div><div className="stat-label">Win rate</div>
            <div className="stat-value">{pctv(s.winRate)}</div></div>
        </div>
      </section>

      <section className="card">
        <h2>Trades</h2>
        {trades.length === 0 ? <div className="muted">No saved trades yet.</div> : (
          <div className="trade-cards">
            {trades.map((t) => {
              const v = t.status === "closed" ? t.pnl : t.last_pnl;
              const isSel = t.id === selectedId;
              return (
                <div className={`trade-card ${isSel ? "selected" : ""}`} key={t.id}
                  style={{ cursor: "pointer" }}
                  onClick={() => setSelectedId(isSel ? null : t.id)}>
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
                    <span className={`pill tiny ${t.status === "open" ? "live" : "idle"}`}>
                      {t.status}{t.exit_reason ? ` · ${t.exit_reason}` : ""}</span>
                    {t.status === "open" &&
                      <button className="btn ghost tiny" onClick={(e) => closeTrade(t.id, e)}>Close</button>}
                  </div>
                  {isSel && (
                    <div className="trade-detail" onClick={(e) => e.stopPropagation()}>
                      {detailErr ? <div className="muted">Couldn't load payoff.</div>
                        : !detail ? <div className="muted">Loading payoff…</div>
                        : (
                          <>
                            <PayoffChart curve={detail.full.curve}
                              breakevens={detail.full.breakevens || []}
                              strikes={t.legs.filter((l) => l.right !== "S").map((l) => l.strike)}
                              spot={detail.spot} height={300} />
                            <div className="deploy-grid" style={{ marginTop: 10 }}>
                              <div><div className="stat-label">Breakevens</div>
                                <div className="stat-value">
                                  {(detail.full.breakevens || []).map((b) => `$${b.toFixed(2)}`).join(" · ") || "—"}</div></div>
                              <div><div className="stat-label">Max profit</div>
                                <div className="stat-value pos">{money(detail.full.max_profit)}</div></div>
                              <div><div className="stat-label">Max loss</div>
                                <div className="stat-value neg">{money(detail.full.max_loss)}</div></div>
                              <div><div className="stat-label">Live P&amp;L</div>
                                <div className={`stat-value ${(v ?? 0) >= 0 ? "pos" : "neg"}`}>{money(v)}</div></div>
                            </div>
                            {detail.greeks && <GreeksPanel greeks={detail.greeks} />}
                          </>
                        )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
