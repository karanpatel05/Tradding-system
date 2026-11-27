import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { humanize, pct, signedUsd, tone, usd } from "../strategyUi.jsx";
import "./savedBacktest.css";

const basisLabels = {
  buy_and_hold_baseline: "Buy-and-hold baseline",
  rule_driven: "Single-symbol replay",
  rule_driven_daily_selection: "Daily selection replay",
  rule_driven_portfolio: "Portfolio replay",
};
const periodOrder = ["1D", "1W", "1M", "YTD", "full"];
const compactUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact" });
const number = (value, digits = 0) => Number.isFinite(value)
  ? value.toLocaleString("en-US", { maximumFractionDigits: digits })
  : "—";

function EquityChart({ points, indexed }) {
  const label = indexed ? "Equity index (1.00 at start)" : "Equity (USD)";
  const indexValue = (value) => Number.isFinite(value) ? value.toFixed(2) : "—";
  const format = indexed ? indexValue : usd;
  const last = points.at(-1);
  return (
    <section className="saved-equity">
      <div className="saved-equity-head">
        <h3>{label}</h3>
        <span>{last.date}<strong>{format(last.equity)}</strong></span>
      </div>
      <ResponsiveContainer width="100%" height={216}>
        <LineChart data={points} accessibilityLayer title={label} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border-soft)" />
          <XAxis dataKey="date" minTickGap={56} tickLine={false} axisLine={{ stroke: "var(--border)" }} tick={{ fontSize: 11 }} />
          <YAxis domain={["auto", "auto"]} width={56} tickFormatter={indexed ? indexValue : compactUsd.format}
            tickLine={false} axisLine={false} tick={{ fontSize: 11 }} />
          <Tooltip formatter={(value) => [format(value), indexed ? "Equity index" : "Equity"]} />
          <Line type="linear" dataKey="equity" stroke="var(--accent)" strokeWidth={2}
            dot={points.length === 1} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </section>
  );
}

function PeriodTable({ periods }) {
  const keys = [...new Set([...periodOrder, ...Object.keys(periods)])].filter((key) => periods[key]);
  return (
    <section>
      <h3>Returns by period</h3>
      <div className="table-scroll">
        <table className="table" aria-label="Buy-and-hold returns by period">
          <thead>
            <tr>
              <th scope="col">Period</th><th scope="col" className="num">Return</th>
              <th scope="col" className="num">Positive days</th><th scope="col" className="num">Average daily return</th>
              <th scope="col" className="num">Max drawdown</th><th scope="col" className="num">Sharpe</th>
              <th scope="col" className="num">Observations</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => {
              const observations = periods[key].trade_count;
              const row = observations === 0 ? {} : periods[key];
              return (
                <tr key={key}>
                  <th scope="row">{key === "full" ? "Full period" : key}</th>
                  <td className={`num ${tone(row.total_return)}`}>{pct(row.total_return, 2)}</td>
                  <td className="num">{pct(row.win_rate, 1)}</td>
                  <td className="num">{pct(row.expectancy, 2)}</td>
                  <td className="num">{pct(row.max_drawdown, 2)}</td>
                  <td className="num">{number(row.sharpe, 2)}</td>
                  <td className="num">{number(observations)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export default function SavedBacktest({ result }) {
  const { metrics, results, timeframes, date_range: range } = result;
  const indexed = !metrics && !result.charts && (result.basis === "buy_and_hold_baseline" || timeframes != null);
  const curve = result.charts?.equity_curve ?? result.equity_curve;
  const points = Array.isArray(curve) ? curve.filter((p) => p?.date && Number.isFinite(p.equity)) : [];
  const context = [
    result.symbol,
    basisLabels[result.basis] || (result.basis && humanize(result.basis)),
    [range?.start, range?.end].filter(Boolean).join(" – "),
    Number.isFinite(range?.trading_days) && `${number(range.trading_days)} ${result.basis === "rule_driven" ? "bars" : "trading days"}`,
  ].filter(Boolean);
  const hasPeriods = timeframes && Object.keys(timeframes).length > 0;
  return (
    <div className="saved-backtest">
      {context.length > 0 && <p className="sd-card-meta">{context.join(" · ")}</p>}
      {metrics && (
        <dl className="saved-metrics">
          <div><dt>Net P&amp;L</dt><dd className={tone(metrics.net_profit)}>{signedUsd(metrics.net_profit)}</dd></div>
          <div><dt>Net return</dt><dd className={tone(metrics.net_return_pct)}>{pct(metrics.net_return_pct, 2)}</dd></div>
          <div><dt>Win rate</dt><dd>{pct(metrics.win_rate, 1)}</dd></div>
          <div><dt>Trades</dt><dd>{number(results?.trades_taken ?? result.trades?.length)}</dd></div>
        </dl>
      )}
      {points.length > 0 && <EquityChart points={points} indexed={indexed} />}
      {hasPeriods && <PeriodTable periods={timeframes} />}
      {metrics && (
        <dl className="saved-stats">
          <div><dt>Average trade</dt><dd>{signedUsd(metrics.expectancy)}</dd></div>
          <div><dt>Profit factor</dt><dd>{number(metrics.profit_factor, 2)}</dd></div>
          <div><dt>Trade-return drawdown</dt><dd>{pct(metrics.max_drawdown, 2)}</dd></div>
        </dl>
      )}
      {result.note && <p className="strat-caption">{result.note}</p>}
      {!metrics && !hasPeriods && !points.length && !result.note && <p className="muted">This saved run contains no performance data.</p>}
    </div>
  );
}
