import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { api } from "../api.js";

const TF_ORDER = ["1D", "1W", "1M", "YTD", "full"];
const pct = (n) =>
  n === null || n === undefined ? "—" : `${(n * 100).toFixed(2)}%`;

export default function StrategyDetail({ keyName, onBack }) {
  const [detail, setDetail] = useState(null);
  const [backtest, setBacktest] = useState(null);
  const [alloc, setAlloc] = useState("");
  const [msg, setMsg] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [backtestError, setBacktestError] = useState(null);

  const load = () =>
    api
      .strategy(keyName)
      .then((d) => {
        setDetail(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(e.message));
  useEffect(() => {
    load();
  }, [keyName]);

  const ready = detail?.overview?.ready;

  useEffect(() => {
    let alive = true;
    if (ready)
      api
        .backtest(keyName)
        .then((d) => {
          if (alive) setBacktest(d);
        })
        .catch((e) => {
          if (alive) setBacktestError(e.message);
        });
    return () => {
      alive = false;
    };
  }, [ready, keyName]);

  const act = async (fn) => {
    setMsg(null);
    try {
      await fn();
      load();
    } catch (err) {
      setMsg(err.message);
    }
  };

  if (!detail)
    return (
      <div>
        <button className="btn ghost" onClick={onBack}>
          ← Back to strategies
        </button>
        <div className={loadError ? "banner error" : "empty"} role="status">
          {loadError
            ? `Could not load strategy: ${loadError}`
            : "Loading strategy…"}
        </div>
      </div>
    );
  const ov = detail.overview;

  return (
    <div className="strategy-detail">
      <button className="btn ghost" onClick={onBack}>
        ← Back
      </button>
      <div className="row between center">
        <h1>{ov.name}</h1>
        {ready ? (
          <span className={`pill ${detail.running ? "live" : "idle"}`}>
            {detail.status}
          </span>
        ) : (
          <span className="pill soon">Coming Soon</span>
        )}
      </div>

      {/* COMING SOON: no overview, no backtest, no controls. */}
      {!ready ? (
        <div className="card coming-soon-big">
          <h2>Coming Soon</h2>
          <p className="muted">
            This strategy's rules haven't been implemented yet. Overview and
            backtest will appear here, and activation / capital allocation will
            unlock, once logic is added.
          </p>
        </div>
      ) : (
        <>
          <section className="card">
            <h2>Overview</h2>
            <p>{ov.description}</p>
            {ov.rules?.length > 0 && (
              <>
                <h4>Rules</h4>
                <ul>
                  {ov.rules.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </>
            )}
            <p>
              <b>Entry:</b> {ov.entry_logic}
            </p>
            <p>
              <b>Exit:</b> {ov.exit_logic}
            </p>
            <p>
              <b>Selection criteria:</b> {ov.selection_criteria}
            </p>
          </section>

          <section className="card">
            <h2>Controls</h2>
            <div className="row gap center">
              <input
                className="input narrow"
                type="number"
                placeholder="Capital $"
                aria-label="Capital allocation in dollars"
                value={alloc}
                onChange={(e) => setAlloc(e.target.value)}
              />
              <button
                className="btn"
                onClick={() => act(() => api.allocate(keyName, Number(alloc)))}
              >
                Set Allocation
              </button>
              {detail.running ? (
                <button
                  className="btn danger"
                  onClick={() => act(() => api.stop(keyName))}
                >
                  Stop
                </button>
              ) : (
                <button
                  className="btn primary"
                  onClick={() => act(() => api.activate(keyName))}
                >
                  Activate
                </button>
              )}
            </div>
            <div className="muted small">
              Current: ${(detail.capital_allocation || 0).toLocaleString()}
            </div>
            <div className="muted small">
              Each trade is sized into <b>whole shares</b> from this allocation
              and the per-trade risk %, capped by allocation ÷ entry price.
              Orders go to Alpaca paper only when{" "}
              <code>ENABLE_LIVE_ORDERS</code> is set server-side (otherwise
              simulated in-app).
            </div>
            {msg && <div className="banner error">{msg}</div>}
          </section>

          <section className="card">
            <h2>Backtest</h2>
            {!backtest?.data_available ? (
              <div className={backtestError ? "banner error" : "muted"}>
                {backtestError || backtest?.note || "Loading backtest…"}
              </div>
            ) : (
              <>
                <div className="muted small">
                  {backtest.symbol} · {backtest.basis} ·{" "}
                  {backtest.date_range.start} → {backtest.date_range.end} (
                  {backtest.date_range.trading_days} trading days)
                </div>
                <table className="table metrics">
                  <thead>
                    <tr>
                      <th>Timeframe</th>
                      <th>Return</th>
                      <th>Win%</th>
                      <th>Expectancy</th>
                      <th>Max DD</th>
                      <th>Sharpe</th>
                      <th>Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {TF_ORDER.filter((tf) => backtest.timeframes[tf]).map(
                      (tf) => {
                        const m = backtest.timeframes[tf];
                        return (
                          <tr key={tf}>
                            <td>{tf}</td>
                            <td className={m.total_return >= 0 ? "pos" : "neg"}>
                              {pct(m.total_return)}
                            </td>
                            <td>{pct(m.win_rate)}</td>
                            <td>{pct(m.expectancy)}</td>
                            <td className="neg">{pct(m.max_drawdown)}</td>
                            <td>{m.sharpe}</td>
                            <td>{m.trade_count}</td>
                          </tr>
                        );
                      },
                    )}
                  </tbody>
                </table>
                <h4>Equity Curve</h4>
                <ResponsiveContainer width="100%" height={240}>
                  <LineChart data={backtest.equity_curve}>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="var(--border)"
                    />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 10 }}
                      minTickGap={40}
                    />
                    <YAxis tick={{ fontSize: 10 }} domain={["auto", "auto"]} />
                    <Tooltip />
                    <Line
                      type="monotone"
                      dataKey="equity"
                      stroke="var(--accent)"
                      dot={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </>
            )}
          </section>
        </>
      )}
    </div>
  );
}
