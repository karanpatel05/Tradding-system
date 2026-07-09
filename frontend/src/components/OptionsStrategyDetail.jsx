import { useEffect, useState } from "react";
import { api } from "../api.js";

// Options-aware strategy page (Triple Calendar): strategy overview on top, controls, then the
// live open-position section at the bottom (legs, P&L, target/stop progress, breach). No
// backtest (options history isn't available from IBKR) — the equity StrategyDetail handles that.

const pct = (n) => (n == null ? "—" : `${(n * 100).toFixed(2)}%`);
const money = (n) => (n == null ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const money0 = (n) => (n == null ? "—" : `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const clamp01 = (n) => Math.max(0, Math.min(1, n));

export default function OptionsStrategyDetail({ keyName, onBack }) {
  const [detail, setDetail] = useState(null); // strategy detail (controls/overview)
  const [op, setOp] = useState(null);         // options-position (structure/live/status)
  const [alloc, setAlloc] = useState("");
  const [contracts, setContracts] = useState("");
  const [msg, setMsg] = useState(null);
  const [tickerRows, setTickerRows] = useState(null); // null until seeded from detail
  useEffect(() => {
    if (detail && tickerRows === null) {
      const rows = (detail.tc_tickers && detail.tc_tickers.length)
        ? detail.tc_tickers
        : [{ ticker: (op?.tickers?.[0]?.ticker) || "QQQ", contracts: detail.contracts ?? 1 }];
      setTickerRows(rows.map((r) => ({ ...r })));
    }
  }, [detail, op, tickerRows]);

  const loadDetail = () => api.strategy(keyName).then(setDetail).catch(() => {});
  const loadOp = () => api.optionsPosition(keyName).then(setOp).catch(() => setOp(null));

  useEffect(() => { loadDetail(); loadOp(); }, [keyName]);
  // Poll the live valuation/status while the page is open.
  useEffect(() => {
    const id = setInterval(loadOp, 15000);
    return () => clearInterval(id);
  }, [keyName]);

  const act = async (fn) => {
    setMsg(null);
    try { await fn(); loadDetail(); loadOp(); }
    catch (err) { setMsg(err.message); }
  };

  if (!detail) return <div className="muted">Loading…</div>;
  const ov = detail.overview;
  const hasPos = op?.has_position;

  return (
    <div className="strategy-detail options-detail">
      <button className="btn ghost" onClick={onBack}>← Back</button>

      <div className="row between center">
        <div className="row gap center">
          <h1>{ov.name}</h1>
          {hasPos && <span className={`pill tiny ${op.paper ? "idle" : "trade"}`}>{op.paper ? "Paper" : "Live"}</span>}
        </div>
        <span className={`pill ${detail.running ? "live" : "idle"}`}>{detail.status}</span>
      </div>

      {/* Overview (top) */}
      <section className="card">
        <h2>Overview</h2>
        <p>{ov.description}</p>
        {ov.rules?.length > 0 && (
          <>
            <h4>Rules</h4>
            <ul>{ov.rules.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </>
        )}
        <p><b>Entry:</b> {ov.entry_logic}</p>
        <p><b>Exit:</b> {ov.exit_logic}</p>
        <p><b>Selection criteria:</b> {ov.selection_criteria}</p>
      </section>

      {/* Controls */}
      <section className="card">
        <h2>Controls</h2>
        <div className="row gap center">
          <input
            className="input narrow"
            type="number"
            placeholder="Capital $"
            value={alloc}
            onChange={(e) => setAlloc(e.target.value)}
          />
          <button className="btn" onClick={() => act(() => api.allocate(keyName, Number(alloc)))}>Set Allocation</button>
          {detail.running ? (
            <button className="btn danger" onClick={() => act(() => api.stop(keyName))}>Stop</button>
          ) : (
            <button className="btn primary" onClick={() => act(() => api.activate(keyName))}>Activate</button>
          )}
        </div>
        <div className="muted small">Current allocation: ${(detail.capital_allocation || 0).toLocaleString()}</div>
        <div className="row gap center" style={{ marginTop: 8 }}>
          <input
            className="input tiny"
            type="number"
            min="1"
            placeholder="Contracts"
            value={contracts}
            onChange={(e) => setContracts(e.target.value)}
          />
          <button className="btn" onClick={() => act(() => api.setContracts(keyName, Math.max(1, Number(contracts) || 1)))}>Set Contracts</button>
          <span className="muted small">Current: {detail.contracts ?? 1} contract{(detail.contracts ?? 1) === 1 ? "" : "s"}</span>
        </div>
        <div className="op-params muted small">
          Target <b>+{pct(op?.profit_target_pct ?? 0.10)}</b> · Stop <b>−{pct(op?.stop_loss_pct ?? 0.25)}</b> ·
          VIX scale below <b>{op?.vix_threshold ?? 13}</b>. Live orders only transmit when{" "}
          <code>ENABLE_LIVE_ORDERS</code> is set server-side (otherwise recorded as paper).
        </div>
        {msg && <div className="banner error">{msg}</div>}
      </section>

      {/* Tickers (multi-ticker config) */}
      {tickerRows && (
        <section className="card">
          <h2>Tickers</h2>
          <div className="muted small">Each ticker runs its own Triple Calendar position at its own contract size.</div>
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Ticker</th><th>Contracts</th><th></th></tr></thead>
              <tbody>
                {tickerRows.map((r, i) => (
                  <tr key={i}>
                    <td><input className="input tiny" value={r.ticker}
                      onChange={(e) => setTickerRows((rs) => rs.map((x, j) => j === i ? { ...x, ticker: e.target.value.toUpperCase() } : x))} /></td>
                    <td><input className="input tiny" type="number" min="1" value={r.contracts}
                      onChange={(e) => setTickerRows((rs) => rs.map((x, j) => j === i ? { ...x, contracts: Number(e.target.value) } : x))} /></td>
                    <td><button className="btn ghost tiny" onClick={() => setTickerRows((rs) => rs.filter((_, j) => j !== i))}>✕</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row gap center">
            <button className="btn ghost" onClick={() => setTickerRows((rs) => [...rs, { ticker: "", contracts: 1 }])}>+ Add ticker</button>
            <button className="btn primary" onClick={() => act(() => api.setTickers(keyName,
              tickerRows.filter((r) => r.ticker.trim()).map((r) => ({ ticker: r.ticker.trim().toUpperCase(), contracts: Math.max(1, Number(r.contracts) || 1) }))))}>Save tickers</button>
          </div>
        </section>
      )}

      {/* Open Position (bottom) */}
      {hasPos ? (
        <section className="card">
          <div className="row between center">
            <h2>Open Position — {op.underlying}</h2>
            <span className="muted small">
              entered {op.entry_time?.slice(0, 10)}{op.days_held != null && ` · ${op.days_held}d held`}
            </span>
          </div>

          <div className="op-value-row">
            <div>
              <div className="muted small">Unrealized P&L</div>
              <div className={`big-pnl ${(op.unrealized_pnl ?? 0) >= 0 ? "pos" : "neg"}`}>
                {op.unrealized_pnl == null ? "—" : money(op.unrealized_pnl)}
                {op.unrealized_pct != null && (
                  <span className="op-pct"> {(op.unrealized_pct * 100).toFixed(2)}%</span>
                )}
              </div>
            </div>
            <div className="op-value-meta">
              <div><span className="muted small">Net debit</span><b className="num">{money0(op.net_debit)}</b></div>
              <div><span className="muted small">Value</span><b className="num">{op.value == null ? "—" : money0(op.value)}</b></div>
              {op.spot != null && <div><span className="muted small">Spot</span><b className="num">{money0(op.spot)}</b></div>}
              {op.valued_at && (
                <div>
                  <span className="muted small">Valued</span>
                  <b className="num">{op.valued_at.slice(11, 16)}{op.stale && <span className="pill tiny warn stale-badge">stale</span>}</b>
                </div>
              )}
            </div>
          </div>

          {/* Target / stop meter — 0 at entry, full at +10% target; stop side to the left. */}
          {op.progress_to_target != null && (
            <div className="op-meter">
              <div className="op-meter-track">
                <div className="op-meter-fill" style={{ width: `${clamp01(op.progress_to_target) * 100}%` }} />
              </div>
              <div className="op-meter-ends">
                <span className={`${(op.distance_to_stop ?? 0) > 0 ? "neg" : "muted"}`}>
                  Stop {money0(op.stop_value)} (−{pct(op.stop_loss_pct)})
                </span>
                <span className="muted">{Math.round(clamp01(op.progress_to_target) * 100)}% to target</span>
                <span className="pos">Target {money0(op.target_value)} (+{pct(op.profit_target_pct)})</span>
              </div>
            </div>
          )}

          <h4>Calendars</h4>
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>#</th><th>Strike</th><th>Sell exp</th><th>DTE</th><th>Buy exp</th><th>DTE</th></tr></thead>
              <tbody>
                {op.calendars.map((c, i) => (
                  <tr key={i}>
                    <td>{i + 1}</td>
                    <td className="num">{c.strike}</td>
                    <td className="num">{c.sell_expiry}</td>
                    <td className="num">{c.sell_dte}</td>
                    <td className="num">{c.buy_expiry}</td>
                    <td className="num">{c.buy_dte}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {op.breach && (
            <div className="op-breach">
              <span className="muted small">Breach guard</span>
              <span className="num">lower {op.breach.lower_strike} <span className={op.breach.dist_lower_pct > 0 ? "neg" : "muted"}>({pct(op.breach.dist_lower_pct)})</span></span>
              <span className="num">upper {op.breach.upper_strike} <span className={op.breach.dist_upper_pct < 0 ? "neg" : "muted"}>({(op.breach.dist_upper_pct * 100).toFixed(2)}%)</span></span>
            </div>
          )}
        </section>
      ) : (
        <section className="card op-empty">
          <div className="op-empty-dot" />
          <div>
            <h2>Open Position</h2>
            <p className="muted">
              No open position — scanning. The loop enters a fresh three-calendar spread on{" "}
              <b>{op?.underlying || "the configured underlying"}</b> when conditions align.
            </p>
            <div className="muted small">{ov.entry_logic}</div>
            {op?.last_trade_time && (
              <div className="muted small">Last trade: {op.last_trade_time.slice(0, 16).replace("T", " ")}</div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
