import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { useSnapshot } from "./hooks/useWebSocket.js";
import Dashboard from "./components/Dashboard.jsx";
import StrategiesTab from "./components/StrategiesTab.jsx";
import CalendarTab from "./components/CalendarTab.jsx";
import ApiTab from "./components/ApiTab.jsx";

// Minimal stroke icons — keeps the sidebar crisp without an icon dependency.
const Icon = ({ name }) => {
  const paths = {
    Dashboard: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
    Strategies: "M13 2L3 14h7l-1 8 10-12h-7l1-8z",
    Journal: "M7 2h10a2 2 0 012 2v16l-7-3-7 3V4a2 2 0 012-2z",
    API: "M12.65 10A6 6 0 1 0 7 14h.65l4.35 4.35V21h3v-3h3v-3h2v-5h-7.35zM7 12a2 2 0 1 1 0-4 2 2 0 0 1 0 4z",
  };
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
      <path d={paths[name]} />
    </svg>
  );
};

const TABS = ["Dashboard", "Strategies", "Journal", "API"];

export default function App() {
  const [auth, setAuth] = useState(null); // null = loading
  const [tab, setTab] = useState("Dashboard");
  const [focusKey, setFocusKey] = useState(null); // strategy to open from the dashboard
  const [connecting, setConnecting] = useState(false);

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

  // Connect the live feed only once the IBKR Gateway is connected.
  const { snapshot, connected } = useSnapshot(auth?.connected);

  if (auth === null) {
    return <div className="boot">Loading…</div>;
  }

  // No hard gate: the app always lands on the Dashboard. Connecting the broker
  // happens in the API tab; unauthenticated views degrade gracefully (no live
  // feed, placeholder market data) until credentials are saved there.
  const disconnect = async () => {
    await api.disconnect().catch(() => {});
    refresh();
  };

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
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">⚡</span>
          <span className="brand-text">Trading<span className="brand-accent">System</span></span>
        </div>

        <nav className="nav">
          {TABS.map((t) => (
            <button
              key={t}
              className={t === tab ? "nav-item active" : "nav-item"}
              onClick={() => setTab(t)}
            >
              <Icon name={t} />
              <span>{t}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="feed-state">
            <span className={connected ? "dot live" : "dot"} />
            <span className="muted small">{connected ? "Live feed" : auth.connected ? "Connecting…" : "Not connected"}</span>
          </div>
          {auth.connected ? (
            <button className="btn ghost full" onClick={disconnect}>Disconnect</button>
          ) : (
            <button className="btn ghost full" disabled={connecting} onClick={connect}>
              {connecting ? "Connecting…" : "Connect IBKR"}
            </button>
          )}
        </div>
      </aside>

      <main className="main">
        {tab === "Dashboard" && (
          <Dashboard
            snapshot={snapshot}
            onOpenStrategy={(key) => { setFocusKey(key); setTab("Strategies"); }}
          />
        )}
        {tab === "Strategies" && (
          <StrategiesTab focusKey={focusKey} onFocusHandled={() => setFocusKey(null)} />
        )}
        {tab === "Journal" && <CalendarTab />}
        {tab === "API" && <ApiTab onChange={refresh} />}
      </main>
    </div>
  );
}
