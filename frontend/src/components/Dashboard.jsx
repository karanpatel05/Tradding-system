import MarketStatus from "./MarketStatus.jsx";
import DataBrokerChips from "./DataBrokerChips.jsx";
import KillSwitch from "./KillSwitch.jsx";
import MarketOverview from "./MarketOverview.jsx";

const fmt = (n, d = 2) =>
  n === null || n === undefined ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });

function Stat({ label, value, sub, tone }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone || ""}`}>{value}</div>
      {sub && <div className="stat-sub muted small">{sub}</div>}
    </div>
  );
}

const SESSION_LABEL = {
  regular: "Open",
  pre_market: "Pre-market",
  after_hours: "After-hours",
  closed: "Closed",
};

export default function Dashboard({ snapshot, onOpenStrategy }) {
  const strategies = snapshot?.strategies || [];
  const positions = snapshot?.positions || [];
  const active = strategies.filter((s) => s.running);
  const activeAlloc = active.reduce((sum, s) => sum + (s.capital_allocation || 0), 0);
  const session = snapshot?.market_status?.session;
  const acct = snapshot?.account;
  const hasAcct = acct && Object.keys(acct).length > 0;
  const pnlTone = (v) => (v === null || v === undefined ? "" : v >= 0 ? "pos" : "neg");

  return (
    <div className="dashboard">
      <header className="page-head">
        <div>
          <h1>Dashboard</h1>
          <MarketStatus status={snapshot?.market_status} />
          <DataBrokerChips />
        </div>
        <KillSwitch />
      </header>

      {/* Summary stats from the live snapshot */}
      <div className="stat-strip">
        <Stat label="Active strategies" value={active.length} sub={`${strategies.length} total`} />
        <Stat label="Capital deployed" value={`$${fmt(activeAlloc, 0)}`} sub="across running" />
        <Stat label="Open positions" value={positions.length} sub={positions.length ? "live" : "flat"} />
        <Stat
          label="Market"
          value={SESSION_LABEL[session] || "—"}
          tone={session === "regular" ? "pos" : session === "closed" ? "neg" : ""}
          sub={snapshot?.market_status ? `${snapshot.market_status.regular_open_et}–${snapshot.market_status.regular_close_et} ET` : ""}
        />
      </div>

      <MarketOverview snapshot={snapshot} />

      <section className="card">
        <div className="card-head">
          <h2>Account</h2>
          <span className="muted small">Alpaca paper · shared account{acct?.currency ? ` · ${acct.currency}` : ""}</span>
        </div>
        {hasAcct ? (
          <div className="stat-strip">
            <Stat label="Net Liquidation" value={`$${fmt(acct.net_liquidation, 0)}`} />
            <Stat label="Cash" value={`$${fmt(acct.total_cash, 0)}`} />
            <Stat label="Buying Power" value={`$${fmt(acct.buying_power, 0)}`} />
            <Stat label="Available Funds" value={`$${fmt(acct.available_funds, 0)}`} />
            <Stat label="Position Value" value={`$${fmt(acct.gross_position_value, 0)}`} />
            <Stat label="Unrealized P&L" value={`$${fmt(acct.unrealized_pnl)}`} tone={pnlTone(acct.unrealized_pnl)} />
            <Stat label="Realized P&L" value={`$${fmt(acct.realized_pnl)}`} tone={pnlTone(acct.realized_pnl)} />
            <Stat label="Init Margin" value={`$${fmt(acct.init_margin, 0)}`} />
            <Stat label="Maint Margin" value={`$${fmt(acct.maint_margin, 0)}`} />
          </div>
        ) : (
          <p className="muted small" style={{ marginTop: 12 }}>Alpaca not connected.</p>
        )}
      </section>

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <h2>Active Strategies</h2>
            <span className="count-pill">{active.length} running</span>
          </div>
          {active.length === 0 ? (
            <div className="empty">
              <div className="empty-icon">◎</div>
              <div className="empty-title">No strategies running</div>
              <div className="muted small">Activate one from the Strategies tab to see it here.</div>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr><th>Strategy</th><th>Status</th><th>Selected</th><th>Capital</th></tr>
              </thead>
              <tbody>
                {active.map((s) => (
                  <tr key={s.key} className="clickable-row"
                    onClick={() => onOpenStrategy?.(s.key)}
                    title="Open strategy">
                    <td>{s.name}</td>
                    <td><span className="pill live">{s.status}</span></td>
                    <td>{s.selected_symbol || "—"}</td>
                    <td className="num">${fmt(s.capital_allocation)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Open Positions <span className="muted small">app-owned only</span></h2>
            <span className="count-pill">{positions.length}</span>
          </div>
          {positions.length === 0 ? (
            <div className="empty">
              <div className="empty-icon">⛶</div>
              <div className="empty-title">No open positions</div>
              <div className="muted small">Positions will appear here once a strategy trades.</div>
            </div>
          ) : (
            <table className="table">
              <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Source</th></tr></thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={i}
                    className={p.strategy_key ? "clickable-row" : ""}
                    onClick={p.strategy_key ? () => onOpenStrategy?.(p.strategy_key) : undefined}
                    title={p.strategy_key ? "Open strategy" : undefined}>
                    <td>{p.symbol}</td>
                    <td className={p.side === "long" ? "pos" : p.side === "short" ? "neg" : ""}>{p.side || "—"}</td>
                    <td className="num">{fmt(p.quantity)}</td>
                    <td className="num">{p.entry_price ?? "—"}</td>
                    <td className="muted small">
                      {p.strategy_key || p.account || "—"}
                      {p.source === "paper" && <span className="pill tiny idle" style={{ marginLeft: 6 }}>paper</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}
