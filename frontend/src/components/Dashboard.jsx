import MarketStatus from "./MarketStatus.jsx";
import MarketOverview from "./MarketOverview.jsx";

const money = (n) =>
  n == null
    ? "—"
    : Number(n).toLocaleString("en-US", { style: "currency", currency: "USD" });
const tone = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
const signed = (n, text) => (n > 0 ? `+${text}` : text);

// Equity the account started the year with. YTD return is measured against it,
// so deposits or withdrawals would show up as return.
const STARTING_EQUITY = 100000;

function Stat({ label, value, sub, color = "", className = "" }) {
  return (
    <div className={`stat ${className}`.trim()}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${color}`}>{value}</div>
      <div className="stat-sub muted">{sub}</div>
    </div>
  );
}

export default function Dashboard({
  snapshot,
  account,
  accountError,
  feedConnected,
  auth,
  onOpenStrategy,
  onConnections,
}) {
  const strategies = snapshot?.strategies || [];
  const positions = snapshot?.positions || [];
  const active = strategies.filter((s) => s.running);
  const allocated = active.reduce(
    (total, s) => total + (s.capital_allocation || 0),
    0,
  );
  const acct = snapshot?.account || account;
  const hasSnapshot = snapshot != null;
  const netLiq = acct?.net_liquidation;
  const ytdReturn = netLiq == null ? null : Number(netLiq) - STARTING_EQUITY;
  const ytdPct = ytdReturn == null ? null : (ytdReturn / STARTING_EQUITY) * 100;
  return (
    <div className="dashboard">
      <header className="page-head">
        <div>
          <h1>Portfolio overview</h1>
          <p className="page-description">
            Your positions, strategies and execution health in one view.
          </p>
        </div>
        <MarketStatus status={snapshot?.market_status} />
      </header>
      {(!feedConnected || !hasSnapshot) && (
        <div className="banner warn" role="status">
          {hasSnapshot
            ? "Feed disconnected. Positions and strategy state show the last received snapshot."
            : "Waiting for a portfolio snapshot. Check Connections for market data and broker status."}
        </div>
      )}
      {accountError && !snapshot?.account && (
        <div className="banner warn" role="status">
          Account balances unavailable: {accountError}.{" "}
          <button className="link-btn" onClick={onConnections}>
            Open Connections
          </button>
        </div>
      )}
      <MarketOverview snapshot={feedConnected ? snapshot : null} />
      <div className="stat-strip operator-metrics has-feature">
        <Stat
          className="stat-feature"
          label="Portfolio YTD return"
          value={ytdReturn == null ? "—" : signed(ytdReturn, money(ytdReturn))}
          sub={
            <>
              {ytdPct != null && (
                <b className={`stat-pct ${tone(ytdPct)}`}>
                  {signed(ytdPct, `${ytdPct.toFixed(2)}%`)}
                </b>
              )}
              {ytdPct != null && " · "}
              from ${STARTING_EQUITY.toLocaleString("en-US")} start
            </>
          }
          color={tone(ytdReturn)}
        />
        <Stat
          label="Net liquidation"
          value={money(acct?.net_liquidation)}
          sub="Alpaca paper · shared account"
        />
        <Stat
          label="Unrealized P&L"
          value={money(acct?.unrealized_pnl)}
          sub="Account positions"
          color={tone(acct?.unrealized_pnl)}
        />
        <Stat
          label="Buying power"
          value={money(acct?.buying_power)}
          sub="Account buying power"
        />
        <Stat
          label="Active strategies"
          value={hasSnapshot ? `${active.length} / ${strategies.length}` : "—"}
          sub={hasSnapshot ? "From portfolio snapshot" : "Awaiting data"}
        />
      </div>
      <div className="operator-grid">
        <div className="operator-primary">
          <section className="card">
            <div className="card-head">
              <h2>Open positions</h2>
              <span className="count-pill">
                {hasSnapshot
                  ? `${positions.length} app-owned`
                  : "Awaiting data"}
              </span>
            </div>
            {!hasSnapshot || positions.length === 0 ? (
              <div className="empty">
                <div className="empty-title">
                  {hasSnapshot ? "No open positions" : "Positions unavailable"}
                </div>
                <p className="small">
                  {hasSnapshot
                    ? "Positions appear here when a strategy trades."
                    : "A portfolio snapshot is required to display positions."}
                </p>
              </div>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Symbol / strategy</th>
                      <th>Side</th>
                      <th>Quantity</th>
                      <th>Entry</th>
                      <th>Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p, i) => (
                      <tr key={`${p.symbol}-${i}`}>
                        <td>
                          {p.strategy_key ? (
                            <button
                              className="link-btn"
                              onClick={() => onOpenStrategy(p.strategy_key)}
                            >
                              {p.symbol}
                            </button>
                          ) : (
                            <b>{p.symbol}</b>
                          )}
                          <div className="small muted">
                            {strategies.find((s) => s.key === p.strategy_key)
                              ?.name ||
                              p.strategy_key ||
                              "—"}
                          </div>
                        </td>
                        <td
                          className={
                            p.side === "long"
                              ? "pos"
                              : p.side === "short"
                                ? "neg"
                                : ""
                          }
                        >
                          {p.side || "—"}
                        </td>
                        <td className="num">{p.quantity ?? "—"}</td>
                        <td className="num">{money(p.entry_price)}</td>
                        <td>
                          <span className="pill idle">{p.source || "—"}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section className="card">
            <div className="card-head">
              <h2>Strategy activity</h2>
              <span className="count-pill">
                {hasSnapshot ? `${active.length} running` : "Awaiting data"}
              </span>
            </div>
            {!active.length ? (
              <div className="empty">
                <div className="empty-title">
                  {hasSnapshot
                    ? "No strategies running"
                    : "Strategy state unavailable"}
                </div>
                <p className="small">
                  Review strategy rules and allocation in Strategies.
                </p>
              </div>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Strategy</th>
                      <th>Selected symbol</th>
                      <th>Allocation</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {active.map((s) => (
                      <tr key={s.key}>
                        <td>
                          <button
                            className="link-btn"
                            onClick={() => onOpenStrategy(s.key)}
                          >
                            {s.name}
                          </button>
                        </td>
                        <td>{s.selected_symbol || "—"}</td>
                        <td className="num">{money(s.capital_allocation)}</td>
                        <td>
                          <span className="pill live">{s.status}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section className="card">
            <div className="card-head">
              <h2>Account balances</h2>
              <span className="small muted">
                {acct?.currency || "USD"} · shared account
              </span>
            </div>
            <dl className="account-details">
              {[
                ["Cash", acct?.total_cash],
                ["Available funds", acct?.available_funds],
                ["Position value", acct?.gross_position_value],
                ["Realized P&L", acct?.realized_pnl],
                ["Initial margin", acct?.init_margin],
                ["Maintenance margin", acct?.maint_margin],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{money(value)}</dd>
                </div>
              ))}
            </dl>
          </section>
        </div>
        <aside className="operator-secondary">
          <section className="card">
            <h2>Capital allocation</h2>
            <div className="allocation-total">
              {hasSnapshot ? money(allocated) : "—"}
              <div className="small muted">budgeted to running strategies</div>
            </div>
            {active.map((s) => (
              <div className="allocation-row" key={s.key}>
                <div>
                  <span>{s.name}</span>
                  <b>{money(s.capital_allocation)}</b>
                </div>
                <div className="allocation-track">
                  <i
                    style={{
                      width: `${allocated > 0 ? Math.min(100, Math.max(0, (s.capital_allocation / allocated) * 100)) : 0}%`,
                    }}
                  />
                </div>
              </div>
            ))}
            <p className="panel-note">
              Strategy budgets are not a measure of actual portfolio exposure.
            </p>
          </section>
          <section className="card">
            <div className="card-head">
              <h2>System health</h2>
              <button className="link-btn" onClick={onConnections}>
                Details
              </button>
            </div>
            <dl className="health-list">
              <div>
                <dt>Snapshot feed</dt>
                <dd>
                  <span className={`pill ${feedConnected ? "live" : "idle"}`}>
                    {feedConnected ? "Connected" : "Offline"}
                  </span>
                </dd>
              </div>
              <div>
                <dt>Market data</dt>
                <dd>Tastytrade</dd>
              </div>
              <div>
                <dt>Execution broker</dt>
                <dd>Alpaca paper</dd>
              </div>
              <div>
                <dt>Recovery state</dt>
                <dd>
                  <span className={`pill ${auth?.safe_mode ? "down" : "idle"}`}>
                    {auth?.safe_mode
                      ? "Safe mode"
                      : auth?.conn_state || "Unknown"}
                  </span>
                </dd>
              </div>
            </dl>
            <p className="panel-note">
              Connection and order-routing details are available in Connections.
            </p>
          </section>
        </aside>
      </div>
    </div>
  );
}
