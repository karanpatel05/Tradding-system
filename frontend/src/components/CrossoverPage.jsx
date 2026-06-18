import { useEffect, useMemo, useState } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../api.js";

const money = (n, d = 2) =>
  n === null || n === undefined ? "—" : `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
const pct = (n) => (n === null || n === undefined ? "—" : `${(n * 100).toFixed(2)}%`);
const num = (n, d = 2) => (n === null || n === undefined ? "—" : Number(n).toFixed(d));
const time = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

const PRESETS = [["1M", 30], ["3M", 90], ["6M", 180], ["YTD", "ytd"], ["1Y", 365], ["2Y", 730]];
const DIRECTIONS = ["long", "short", "both"];
const OBJECTIVES = [
  ["net_profit", "Net Profit"], ["sharpe", "Sharpe"], ["profit_factor", "Profit Factor"],
  ["win_rate", "Win Rate"], ["expectancy", "Expectancy"], ["avg_r_multiple", "Avg R"],
  ["max_drawdown", "Max Drawdown"], ["trade_count", "Trade Count"],
];
const isoDay = (d) => d.toISOString().slice(0, 10);
const csv = (s) => s.split(",").map((x) => parseFloat(x.trim())).filter((x) => !Number.isNaN(x));

export default function CrossoverPage({ keyName, onBack }) {
  const [detail, setDetail] = useState(null);
  const [health, setHealth] = useState(null);
  const [risk, setRisk] = useState(null);
  const [positions, setPositions] = useState([]);
  const [bt, setBt] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [alloc, setAlloc] = useState("");
  const [progress, setProgress] = useState(null);
  const [loadErr, setLoadErr] = useState(null);

  const [cfg, setCfg] = useState({
    capital: 2000, rr: 2.0, stop_atr_mult: 1.5, vol_multiplier: 1.5,
    direction: "both", start: "", end: "", preset: "all",
  });

  const loadLive = () => {
    api.strategy(keyName).then((d) => { setDetail(d); setLoadErr(null); })
      .catch((e) => setLoadErr(e.message || "Failed to load strategy"));
    api.risk(keyName).then(setRisk).catch(() => {});
    api.strategyPositions(keyName).then((d) => setPositions(d.positions || [])).catch(() => {});
  };
  useEffect(() => {
    loadLive();
    api.health().then(setHealth).catch(() => {});
    api.backtestLast(keyName).then((r) => { if (r && r.data_available) setBt(r); }).catch(() => {});
  }, [keyName]);
  useEffect(() => {
    const id = setInterval(loadLive, 4000);
    return () => clearInterval(id);
  }, [keyName]);

  const ov = detail?.overview;
  const mode = !detail?.running ? "Disabled" : health?.live_orders_enabled ? "Live" : "Paper";

  const act = async (fn, okMsg) => {
    setMsg(null); setBusy(true);
    try { await fn(); loadLive(); if (okMsg) setMsg(okMsg); }
    catch (e) { setMsg(e.message); }
    finally { setBusy(false); }
  };

  const applyPreset = (label, days) => {
    const end = new Date();
    let start;
    if (days === "ytd") start = new Date(end.getFullYear(), 0, 1);
    else { start = new Date(); start.setDate(end.getDate() - days); }
    setCfg((c) => ({ ...c, preset: label, start: isoDay(start), end: isoDay(end) }));
  };

  const runBacktest = async () => {
    setMsg(null); setBusy(true); setBt(null); setProgress(null);
    try {
      const result = await api.backtestStream(keyName, {
        capital: cfg.capital, rr: cfg.rr, stop_atr_mult: cfg.stop_atr_mult,
        vol_multiplier: cfg.vol_multiplier, direction: cfg.direction,
        start: cfg.start, end: cfg.end,
      }, (p) => setProgress(p));
      setBt(result);
    } catch (e) { setMsg(e.message); }
    finally { setBusy(false); setProgress(null); }
  };

  const pctDone = progress ? Math.round((progress.scanned / progress.total) * 100) : 0;

  if (!detail) return (
    <div className="ema-page">
      <button className="btn ghost" onClick={onBack}>← Back</button>
      {loadErr ? <div className="banner error">Couldn’t load strategy: {loadErr}</div>
        : <div className="muted">Loading…</div>}
    </div>
  );

  const direction = detail.direction || "both";

  return (
    <div className="ema-page">
      <button className="btn ghost" onClick={onBack}>← Back</button>

      {/* OVERVIEW */}
      <section className="card">
        <div className="card-head">
          <h1>{ov?.name || "9-15 EMA Crossover"}</h1>
          <span className={`mode-badge ${mode.toLowerCase()}`}>{mode}</span>
        </div>
        <p className="muted">{ov?.description}</p>
        <div className="chip-row">
          {["Multi-Position", "EMA9/15 Crossover", "Long + Short", "Volume Confirm",
            "Risk-Based Sizing", "Optional 15m HTF"].map((u) => <span className="chip" key={u}>{u}</span>)}
        </div>
      </section>

      {/* LIVE PORTFOLIO DASHBOARD */}
      <section className="card">
        <div className="card-head">
          <h2>Live Portfolio</h2>
          <span className={`pill ${detail.running ? "live" : "idle"}`}>
            {detail.running ? "Running" : (detail.status || "idle")}
          </span>
        </div>
        {risk?.halted && (
          <div className="banner warn">Daily-loss limit hit — new entries are halted until the next session.</div>
        )}
        <div className="deploy-grid">
          <RiskStat label="Open positions" value={`${risk?.open_positions ?? 0} / ${risk?.max_positions ?? "—"}`} />
          <RiskStat label="Capital used" value={`${money(risk?.used_cash, 0)} / ${money(risk?.equity, 0)}`} />
          <RiskStat label="Open risk" value={`${money(risk?.used_risk, 0)} / ${money(risk?.max_risk, 0)}`} />
          <RiskStat label="Trades today" value={`${risk?.trades_today ?? 0} / ${risk?.max_trades_per_day ?? "—"}`} />
          <RiskStat label="Realized today" value={money(risk?.realized_today)}
            tone={(risk?.realized_today ?? 0) >= 0 ? "pos" : "neg"} />
          <RiskStat label="Remaining cash" value={money(risk?.remaining_cash, 0)} />
        </div>

        <div className="row gap center" style={{ marginTop: 14, flexWrap: "wrap" }}>
          <span className="stat-label">Direction</span>
          {DIRECTIONS.map((d) => (
            <button key={d} disabled={busy}
              className={`chip btn-chip ${direction === d ? "active" : ""}`}
              onClick={() => act(() => api.setDirection(keyName, d), `Direction set to ${d}`)}>{d}</button>
          ))}
          <span className="muted small">gates which sides may open — open positions keep their own exits</span>
        </div>

        <h4 style={{ marginTop: 16 }}>Open Positions ({positions.length})</h4>
        {positions.length === 0 ? (
          <div className="muted small">Flat — no open positions.</div>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Notional</th><th>Since</th></tr></thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol}>
                    <td>{p.symbol}</td>
                    <td className={p.side === "long" ? "pos" : "neg"}>{p.side}</td>
                    <td className="num">{p.quantity}</td>
                    <td className="num">{num(p.entry_price)}</td>
                    <td className="num">{num(p.stop)}</td>
                    <td className="num">{num(p.target)}</td>
                    <td className="num">{money(p.notional, 0)}</td>
                    <td className="num">{time(p.entry_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* DEPLOYMENT CONTROLS */}
      <section className="card">
        <h2>Deployment Controls</h2>
        <div className="deploy-grid">
          <div><div className="stat-label">Current mode</div><div className={`stat-value ${mode === "Live" ? "neg" : mode === "Paper" ? "pos" : ""}`}>{mode}</div></div>
          <div><div className="stat-label">Broker connected</div><div className="stat-value">{detail.broker_connected ? "Yes" : "No"}</div></div>
          <div><div className="stat-label">Last signal</div><div className="stat-sub">{time(detail.last_signal_time)}</div></div>
          <div><div className="stat-label">Last trade</div><div className="stat-sub">{time(detail.last_trade_time)}</div></div>
        </div>
        <div className="row gap center" style={{ flexWrap: "wrap", marginTop: 12 }}>
          <input className="input narrow" type="number" placeholder="Allocation $"
            value={alloc} onChange={(e) => setAlloc(e.target.value)} />
          <button className="btn" disabled={busy}
            onClick={() => act(() => api.allocate(keyName, Number(alloc)), "Allocation set")}>Set Allocation</button>
          {!detail.running ? (
            <button className="btn primary" disabled={busy}
              onClick={() => act(() => api.activate(keyName),
                health?.live_orders_enabled ? "Deployed LIVE — real orders active" :
                "Started — orders stay PAPER until ENABLE_LIVE_ORDERS=true server-side")}>
              {health?.live_orders_enabled ? "Deploy Live" : "Start (Paper)"}
            </button>
          ) : (
            <button className="btn danger" disabled={busy}
              onClick={() => act(() => api.stop(keyName), "Stopped")}>Stop Trading</button>
          )}
          <span className="muted small">Allocated: {money(detail.capital_allocation, 0)} — the shared buying-power pool across positions</span>
        </div>
        {!health?.live_orders_enabled && (
          <div className="banner warn">
            Live order transmission is OFF (<code>ENABLE_LIVE_ORDERS=false</code>). The strategy runs
            fully and records paper trades; set the flag server-side to send real orders.
          </div>
        )}
        {msg && <div className="banner ok">{msg}</div>}
      </section>

      {/* RULES */}
      {ov?.rules?.length > 0 && (
        <section className="card">
          <h2>Strategy Rules</h2>
          <div className="rule-meta">
            <span><b>Timeframe:</b> 5 Minute</span>
            <span><b>Indicators:</b> EMA 9, EMA 15, ATR(14)</span>
          </div>
          <ul className="rule-list">{ov.rules.map((r, i) => <li key={i}>{r}</li>)}</ul>
          <p><b>Entry:</b> {ov.entry_logic}</p>
          <p><b>Exit:</b> {ov.exit_logic}</p>
        </section>
      )}

      {/* BACKTEST CONFIG */}
      <section className="card">
        <h2>Backtest Configuration</h2>
        <div className="cfg-grid">
          <label className="label">Capital (pool)<input className="input" type="number" value={cfg.capital}
            onChange={(e) => setCfg({ ...cfg, capital: Number(e.target.value) })} /></label>
          <label className="label">Risk:Reward<input className="input" type="number" step="0.5" value={cfg.rr}
            onChange={(e) => setCfg({ ...cfg, rr: Number(e.target.value) })} /></label>
          <label className="label">Stop ×ATR<input className="input" type="number" step="0.25" value={cfg.stop_atr_mult}
            onChange={(e) => setCfg({ ...cfg, stop_atr_mult: Number(e.target.value) })} /></label>
          <label className="label">Vol multiplier<input className="input" type="number" step="0.1" value={cfg.vol_multiplier}
            onChange={(e) => setCfg({ ...cfg, vol_multiplier: Number(e.target.value) })} /></label>
          <label className="label">Direction
            <select className="input" value={cfg.direction} onChange={(e) => setCfg({ ...cfg, direction: e.target.value })}>
              {DIRECTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
          <label className="label">Start<input className="input" type="date" value={cfg.start}
            onChange={(e) => setCfg({ ...cfg, start: e.target.value, preset: "custom" })} /></label>
          <label className="label">End<input className="input" type="date" value={cfg.end}
            onChange={(e) => setCfg({ ...cfg, end: e.target.value, preset: "custom" })} /></label>
        </div>
        <div className="chip-row" style={{ marginTop: 10 }}>
          {PRESETS.map(([label, days]) => (
            <button key={label} className={`chip btn-chip ${cfg.preset === label ? "active" : ""}`}
              onClick={() => applyPreset(label, days)}>{label}</button>
          ))}
        </div>
        <div className="muted small" style={{ marginTop: 8 }}>
          Portfolio backtest: the whole watchlist run together through one shared, risk-sized
          buying-power pool — the same simulator and risk engine the live worker uses (sizing parity).
          Reads cached candle data; sign in and let history cache on the first run.
        </div>
        <button className="btn primary" style={{ marginTop: 12 }} disabled={busy} onClick={runBacktest}>
          {busy ? "Running…" : "Run Backtest"}
        </button>
        {busy && progress && (
          <div className="bt-progress">
            <div className="bt-progress-track">
              <div className="bt-progress-fill" style={{ width: `${pctDone}%` }} />
            </div>
            <div className="muted small bt-progress-label">
              Fetching {progress.scanned} / {progress.total} · {progress.symbol} ({pctDone}%)
            </div>
          </div>
        )}
      </section>

      {bt && <BacktestOutput bt={bt} />}

      {/* OPTIMIZER */}
      <Optimizer keyName={keyName} capital={cfg.capital} start={cfg.start} end={cfg.end}
        onApply={(params, dir) => setCfg((c) => ({
          ...c, direction: dir,
          rr: params.risk_reward ?? c.rr,
          stop_atr_mult: params.stop_atr_mult ?? c.stop_atr_mult,
          vol_multiplier: params.vol_multiplier ?? c.vol_multiplier,
        }))} />
    </div>
  );
}

function Optimizer({ keyName, capital, start, end, onApply }) {
  const [grid, setGrid] = useState({ risk_reward: "1.5, 2, 2.5, 3", stop_atr_mult: "1, 1.5, 2", vol_multiplier: "1.2, 1.5, 2" });
  const [dirs, setDirs] = useState({ long: true, short: true, both: true });
  const [objective, setObjective] = useState("net_profit");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(null);
  const [err, setErr] = useState(null);

  const run = async () => {
    setBusy(true); setErr(null); setRes(null); setProgress(null);
    try {
      const body = {
        objective, capital, start, end,
        directions: DIRECTIONS.filter((d) => dirs[d]),
        grid: {
          risk_reward: csv(grid.risk_reward),
          stop_atr_mult: csv(grid.stop_atr_mult),
          vol_multiplier: csv(grid.vol_multiplier),
        },
      };
      const r = await api.optimizeStream(keyName, body, (p) => setProgress(p));
      setRes(r);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); setProgress(null); }
  };

  const rows = res?.results || [];
  const pctDone = progress ? Math.round((progress.scanned / progress.total) * 100) : 0;

  return (
    <section className="card">
      <div className="card-head">
        <h2>Parameter Optimizer</h2>
        <span className="muted small">grid sweep · fetch-once / sim-many · ranked by objective</span>
      </div>
      <div className="cfg-grid">
        <label className="label">Risk:Reward grid<input className="input" value={grid.risk_reward}
          onChange={(e) => setGrid({ ...grid, risk_reward: e.target.value })} /></label>
        <label className="label">Stop ×ATR grid<input className="input" value={grid.stop_atr_mult}
          onChange={(e) => setGrid({ ...grid, stop_atr_mult: e.target.value })} /></label>
        <label className="label">Vol multiplier grid<input className="input" value={grid.vol_multiplier}
          onChange={(e) => setGrid({ ...grid, vol_multiplier: e.target.value })} /></label>
        <label className="label">Objective
          <select className="input" value={objective} onChange={(e) => setObjective(e.target.value)}>
            {OBJECTIVES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
      </div>
      <div className="row gap center" style={{ marginTop: 10, flexWrap: "wrap" }}>
        <span className="stat-label">Directions</span>
        {DIRECTIONS.map((d) => (
          <label key={d} className="chip" style={{ cursor: "pointer" }}>
            <input type="checkbox" checked={dirs[d]} onChange={() => setDirs({ ...dirs, [d]: !dirs[d] })} /> {d}
          </label>
        ))}
        <button className="btn primary" disabled={busy} onClick={run}>{busy ? "Optimizing…" : "Run Optimizer"}</button>
      </div>
      {busy && progress && (
        <div className="bt-progress">
          <div className="bt-progress-track"><div className="bt-progress-fill" style={{ width: `${pctDone}%` }} /></div>
          <div className="muted small bt-progress-label">Fetching {progress.scanned} / {progress.total} · {progress.symbol}</div>
        </div>
      )}
      {err && <div className="banner error">{err}</div>}
      {res && rows.length === 0 && <div className="muted small">{res.note || "No results."}</div>}
      {rows.length > 0 && (
        <div className="table-scroll" style={{ marginTop: 12 }}>
          <table className="table">
            <thead>
              <tr>
                <th>#</th><th>Direction</th><th>R:R</th><th>Stop×ATR</th><th>Vol×</th>
                <th>Objective</th><th>Net P&L</th><th>Win%</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th></th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 25).map((r) => {
                const m = r.metrics || {};
                const ov = r.objective_value;
                return (
                  <tr key={r.rank} className={r.rank === 1 ? "expandable" : ""}>
                    <td className="num">{r.rank}</td>
                    <td className={r.direction === "short" ? "neg" : r.direction === "long" ? "pos" : ""}>{r.direction}</td>
                    <td className="num">{r.params?.risk_reward ?? "—"}</td>
                    <td className="num">{r.params?.stop_atr_mult ?? "—"}</td>
                    <td className="num">{r.params?.vol_multiplier ?? "—"}</td>
                    <td className="num"><b>{ov === null || ov === undefined ? "∞" : num(ov, 2)}</b></td>
                    <td className={`num ${m.net_profit >= 0 ? "pos" : "neg"}`}>{money(m.net_profit, 0)}</td>
                    <td className="num">{pct(m.win_rate)}</td>
                    <td className="num">{num(m.sharpe, 2)}</td>
                    <td className="num neg">{pct(m.max_drawdown)}</td>
                    <td className="num">{r.trade_count}</td>
                    <td><button className="linklike" onClick={() => onApply(r.params || {}, r.direction)}>Apply →</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="muted small" style={{ marginTop: 6 }}>
            {res.combos} combinations swept · objective: {res.objective}. “Apply” loads a row into the backtest config.
          </div>
        </div>
      )}
    </section>
  );
}

function BacktestOutput({ bt }) {
  const m = bt.metrics || {};
  const r = bt.results || {};
  const charts = bt.charts || {};
  const winLoss = useMemo(() => ({ w: r.winning_trades || 0, l: r.losing_trades || 0 }), [r]);

  if (!bt.data_available) {
    return (
      <section className="card">
        <h2>Backtest Results</h2>
        <div className="muted">{bt.note || "No data / no qualifying setups for this configuration."}</div>
      </section>
    );
  }

  return (
    <>
      <section className="card">
        <h2>Backtest Results</h2>
        <div className="muted small">
          {bt.symbol} · {bt.config_used?.direction || "both"} · {bt.date_range?.start} → {bt.date_range?.end} · {bt.date_range?.trading_days} trading days
        </div>
        <div className="stat-strip" style={{ marginTop: 12 }}>
          <Stat label="Watchlist" value={r.stocks_scanned} />
          <Stat label="Trades taken" value={r.trades_taken} />
          <Stat label="Win / Loss" value={`${winLoss.w} / ${winLoss.l}`} />
          <Stat label="Avg hold" value={`${num(m.avg_hold, 0)} min`} />
        </div>
      </section>

      <section className="card">
        <h2>Performance Metrics</h2>
        <div className="metric-grid">
          <Metric label="Net Profit" value={money(m.net_profit)} tone={m.net_profit >= 0 ? "pos" : "neg"} />
          <Metric label="Net Return" value={pct(m.net_return_pct)} tone={m.net_return_pct >= 0 ? "pos" : "neg"} />
          <Metric label="Win Rate" value={pct(m.win_rate)} />
          <Metric label="Profit Factor" value={m.profit_factor ?? "∞"} />
          <Metric label="Sharpe" value={num(m.sharpe, 2)} />
          <Metric label="Expectancy" value={money(m.expectancy)} />
          <Metric label="Avg Win" value={money(m.avg_win)} tone="pos" />
          <Metric label="Avg Loss" value={money(m.avg_loss)} tone="neg" />
          <Metric label="Avg R Multiple" value={num(m.avg_r_multiple, 2) + "R"} />
          <Metric label="Avg Hold" value={num(m.avg_hold, 0) + " min"} />
          <Metric label="Max Drawdown" value={pct(m.max_drawdown)} tone="neg" />
          <Metric label="Best Trade" value={money(m.best_trade)} tone="pos" />
          <Metric label="Worst Trade" value={money(m.worst_trade)} tone="neg" />
          <Metric label="Avg Capital Util." value={pct(m.avg_capital_utilization)} />
        </div>
      </section>

      <section className="card">
        <h2>Charts &amp; Analytics</h2>
        <div className="grid-2">
          <ChartBox title="Equity (dollar pool)">
            <AreaChart data={charts.equity_curve}>
              <CartesianGrid strokeDasharray="3 3" stroke="#222a38" />
              <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
              <YAxis tick={{ fontSize: 10 }} domain={["auto", "auto"]} />
              <Tooltip />
              <Area type="monotone" dataKey="equity" stroke="#6366f1" fill="#6366f133" />
            </AreaChart>
          </ChartBox>
          <ChartBox title="Drawdown ($)">
            <AreaChart data={charts.drawdown}>
              <CartesianGrid strokeDasharray="3 3" stroke="#222a38" />
              <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              <Area type="monotone" dataKey="drawdown" stroke="#f87171" fill="#f8717133" />
            </AreaChart>
          </ChartBox>
          <ChartBox title="Daily P&L">
            <BarChart data={charts.daily_pnl}>
              <CartesianGrid strokeDasharray="3 3" stroke="#222a38" />
              <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              <Bar dataKey="pnl" fill="#34d399" />
            </BarChart>
          </ChartBox>
          <ChartBox title="Monthly P&L">
            <BarChart data={charts.monthly}>
              <CartesianGrid strokeDasharray="3 3" stroke="#222a38" />
              <XAxis dataKey="period" tick={{ fontSize: 10 }} minTickGap={20} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              <Bar dataKey="pnl" fill="#60a5fa" />
            </BarChart>
          </ChartBox>
        </div>
      </section>

      <section className="card">
        <h2>Trade History</h2>
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Date</th><th>Symbol</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Shares</th>
                <th>Capital</th><th>Stop</th><th>Target</th><th>PnL</th><th>R</th><th>Hold</th><th>Result</th>
              </tr>
            </thead>
            <tbody>
              {bt.trades.map((t, i) => (
                <tr key={i}>
                  <td className="num">{t.date}</td>
                  <td>{t.symbol}</td>
                  <td className={t.side === "long" ? "pos" : "neg"}>{t.side}</td>
                  <td className="num">{num(t.entry_price)}</td>
                  <td className="num">{num(t.exit_price)}</td>
                  <td className="num">{t.shares}</td>
                  <td className="num">{money(t.capital_used, 0)}</td>
                  <td className="num">{num(t.stop)}</td>
                  <td className="num">{num(t.target)}</td>
                  <td className={`num ${t.pnl >= 0 ? "pos" : "neg"}`}>{money(t.pnl)}</td>
                  <td className="num">{num(t.r_multiple, 2)}R</td>
                  <td className="num">{t.hold_minutes == null ? "—" : `${num(t.hold_minutes, 0)}m`}</td>
                  <td><span className={`pill ${t.pnl >= 0 ? "live" : "idle"}`}>{t.reason}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

const Stat = ({ label, value }) => (
  <div className="stat"><div className="stat-label">{label}</div><div className="stat-value">{value}</div></div>
);
const RiskStat = ({ label, value, tone }) => (
  <div><div className="stat-label">{label}</div><div className={`stat-value ${tone || ""}`}>{value}</div></div>
);
const Metric = ({ label, value, tone }) => (
  <div className="metric"><div className="metric-label">{label}</div><div className={`metric-value ${tone || ""}`}>{value}</div></div>
);
const ChartBox = ({ title, children }) => (
  <div className="chart-box">
    <div className="chart-title">{title}</div>
    <ResponsiveContainer width="100%" height={200}>{children}</ResponsiveContainer>
  </div>
);
