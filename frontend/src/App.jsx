import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { useSnapshot } from "./hooks/useWebSocket.js";
import ChartWorkspace from "./components/charts/ChartWorkspace.jsx";
import Dashboard from "./components/Dashboard.jsx";
import StrategiesTab from "./components/StrategiesTab.jsx";
import CalendarTab from "./components/CalendarTab.jsx";
import ApiTab from "./components/ApiTab.jsx";
import BuilderTab from "./components/BuilderTab.jsx";
import SafeModeBanner from "./components/SafeModeBanner.jsx";
import ConnectionBanner from "./components/ConnectionBanner.jsx";
import KillSwitch from "./components/KillSwitch.jsx";
import DataBrokerChips from "./components/DataBrokerChips.jsx";

// Minimal stroke icons — keeps the sidebar crisp without an icon dependency.
const Icon = ({ name }) => {
  const paths = {
    Chart: "M3 3h2v16h16v2H3V3zm4 11 4-5 4 2 5-7 2 1-6 9-4-2-4 5z",
    Dashboard: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
    Strategies: "M13 2L3 14h7l-1 8 10-12h-7l1-8z",
    Journal: "M7 2h10a2 2 0 012 2v16l-7-3-7 3V4a2 2 0 012-2z",
    API: "M12.65 10A6 6 0 1 0 7 14h.65l4.35 4.35V21h3v-3h3v-3h2v-5h-7.35zM7 12a2 2 0 1 1 0-4 2 2 0 0 1 0 4z",
    Builder: "M4 4h16v4H4V4zm0 6h10v4H4v-4zm0 6h16v4H4v-4z",
  };
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
};

const NAV_LABELS = {
  Dashboard: "Overview",
  Chart: "Chart",
  Strategies: "Strategies",
  Builder: "Options builder",
  Journal: "Journal",
  API: "Connections",
};

export default function App() {
  const [auth, setAuth] = useState(null); // null = loading
  const [strategyFocus, setStrategyFocus] = useState(() => {
    const key = new URLSearchParams(window.location.search).get("backtest");
    return key && /^[a-z0-9_]+$/.test(key) ? { key, view: "backtests" } : null;
  });
  const [tab, setTab] = useState(strategyFocus ? "Strategies" : "Dashboard");
  const [builderSymbol, setBuilderSymbol] = useState(null); // set by the chart's "Open in options builder"
  const [connecting, setConnecting] = useState(false);
  const [acct, setAcct] = useState(null);
  const [accountError, setAccountError] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .account()
        .then((d) => {
          if (!alive) return;
          setAcct(d.account && typeof d.account === "object" ? d.account : d);
          setAccountError(null);
        })
        .catch((e) => {
          if (alive) {
            setAcct(null);
            setAccountError(e.message);
          }
        });
    load();
    const id = setInterval(load, 60000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      setAuth(await api.authStatus());
    } catch {
      setAuth({ connected: false });
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000); // keep connection state live across the app
    return () => clearInterval(id);
  }, [refresh]);

  // Auto-connect once on page load if not already connected. The backend supervisor
  // keeps retrying after this, so the frontend never loops connect().
  const kickedRef = useRef(false);
  useEffect(() => {
    if (kickedRef.current || auth === null) return;
    const ready =
      auth.conn_state === "ready" ||
      auth.conn_state === "connected" ||
      auth.connected;
    if (!ready) {
      kickedRef.current = true;
      api
        .connect()
        .catch(() => {})
        .finally(refresh);
    }
  }, [auth, refresh]);

  // Connect the live feed only once Tastytrade is verified.
  const { snapshot, connected } = useSnapshot(auth?.connected);

  if (auth === null) {
    return <div className="boot">Loading…</div>;
  }

  // Connect directly from the sidebar. Only fall back to the API tab if the
  // connection fails, so the user can read the error detail there.
  const connect = async () => {
    setConnecting(true);
    try {
      const res = await api.connect();
      if (!res?.connected) setTab("API");
    } catch {
      setTab("API");
    } finally {
      setConnecting(false);
      refresh();
    }
  };

  return (
    <>
      <a className="skip-link" href="#workspace">
        Skip to workspace
      </a>
      <div className="layout">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              ▥
            </span>
            <span className="brand-text">
              Trading<span className="brand-accent">System</span>
            </span>
          </div>

          <div className="workspace-label">Trading workspace</div>
          <nav className="nav" aria-label="Main navigation">
            {Object.keys(NAV_LABELS).map((t) => (
              <button
                key={t}
                title={NAV_LABELS[t]}
                aria-label={NAV_LABELS[t]}
                className={t === tab ? "nav-item active" : "nav-item"}
                aria-current={t === tab ? "page" : undefined}
                onClick={() => setTab(t)}
              >
                <Icon name={t} />
                <span>{NAV_LABELS[t]}</span>
              </button>
            ))}
          </nav>

          <div className="side-foot">
            <div className="avatar">
              {(acct?.account || "T").replace(/[^A-Za-z0-9]/g, "").slice(-1) ||
                "T"}
            </div>
            <div className="side-foot-meta">
              <div className="side-foot-acct">{acct?.account || "—"}</div>
              <div className="side-foot-nl num">
                {acct?.net_liquidation != null
                  ? `$${Number(acct.net_liquidation).toLocaleString()}`
                  : "—"}
              </div>
            </div>
            <span
              className={`dot ${auth?.connected ? "on" : ""}`}
              title={
                auth?.connected ? "Broker verified" : "Broker not verified"
              }
            />
          </div>

          <div className="sidebar-foot">
            <div className="feed-state">
              <span className={connected ? "dot live" : "dot"} />
              <span className="muted small">
                {connected
                  ? "Live feed"
                  : auth.connected
                    ? "Connecting…"
                    : "Not connected"}
              </span>
            </div>
            {!auth.connected && (
              <button
                className="btn ghost full"
                disabled={connecting}
                onClick={connect}
              >
                {connecting ? "Connecting…" : "Connect market data"}
              </button>
            )}
          </div>
        </aside>

        <div className="operator-workspace">
          <header className="operator-toolbar">
            <div className="operator-title">
              Operator console <span className="pill idle">Alpaca paper</span>
            </div>
            <div className="operator-tools">
              <span className="small muted">
                <span
                  className={connected && auth.connected ? "dot live" : "dot"}
                />{" "}
                {connected && auth.connected
                  ? "Feed connected"
                  : "Feed offline"}
              </span>
              <KillSwitch />
            </div>
          </header>
          <SafeModeBanner auth={auth} onResume={refresh} />
          <ConnectionBanner auth={auth} />
          <main className="main" id="workspace" tabIndex={-1}>
            <div className="workspace-breadcrumb">
              Workspace / {NAV_LABELS[tab]}
            </div>
            {tab === "Chart" && (
              <ChartWorkspace
                account={acct}
                accountError={accountError}
                onOpenBuilder={(symbol) => {
                  setBuilderSymbol(symbol);
                  setTab("Builder");
                }}
              />
            )}
            {tab === "Dashboard" && (
              <Dashboard
                snapshot={snapshot}
                account={acct}
                accountError={accountError}
                feedConnected={connected && auth.connected}
                auth={auth}
                onConnections={() => setTab("API")}
                onOpenStrategy={(key) => {
                  setStrategyFocus({ key, view: "overview" });
                  setTab("Strategies");
                }}
              />
            )}
            {tab === "Strategies" && (
              <StrategiesTab
                focus={strategyFocus}
                onFocusHandled={() => setStrategyFocus(null)}
              />
            )}
            {tab === "Journal" && <CalendarTab />}
            {tab === "Builder" && <BuilderTab initialUnderlying={builderSymbol} />}
            {tab === "API" && <ApiTab onChange={refresh} />}
            <footer className="operator-footer">
              <span>TradingSystem</span>
              <DataBrokerChips />
            </footer>
          </main>
        </div>
      </div>
    </>
  );
}
