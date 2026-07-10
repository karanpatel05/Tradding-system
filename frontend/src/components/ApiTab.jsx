import { useEffect, useState } from "react";
import { api } from "../api.js";

// IBKR connection tab: shows the IB Gateway connection config (host/port/clientId/
// account) read from the server, a live connection-state badge, and a Connect
// button (no manual Disconnect — the system is always-connect). No secrets to enter:
// happens in IBKR's own Gateway software; this just opens/closes the local socket.
export default function ApiTab({ onChange }) {
  const [info, setInfo] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      setInfo(await api.authStatus());
    } catch (e) {
      setErr(e.message);
    }
  };
  useEffect(() => {
    load();
    const id = setInterval(load, 5000); // keep the badge live
    return () => clearInterval(id);
  }, []);

  const [tt, setTt] = useState(null);
  const [ttSecret, setTtSecret] = useState("");
  const [ttRefresh, setTtRefresh] = useState("");
  const [ttMsg, setTtMsg] = useState(null);
  const loadTt = () => api.ttStatus().then(setTt).catch(() => {});
  useEffect(() => { loadTt(); }, []);
  const saveTt = async () => {
    setTtMsg(null);
    try {
      const body = {};
      if (ttSecret.trim()) body.provider_secret = ttSecret.trim();
      if (ttRefresh.trim()) body.refresh_token = ttRefresh.trim();
      const res = await api.ttSave(body);
      setTt(res); setTtSecret(""); setTtRefresh("");
      setTtMsg(res.configured ? "Saved — Tastytrade data enabled." : "Saved.");
    } catch (e) { setTtMsg(e.message || "Save failed"); }
  };

  const connect = async () => {
    setErr(null); setBusy(true);
    try {
      const res = await api.connect();
      setInfo(res);
      if (!res.connected) setErr(res.detail || "Could not connect to IB Gateway.");
      onChange?.();
    } catch (e) {
      setErr(e.message || "Connect failed");
    } finally {
      setBusy(false);
    }
  };


  const connected = !!info?.connected;
  const state = info?.conn_state || (connected ? "connected" : "disconnected");
  const STATE_LABEL = { disconnected: "Disconnected", connecting: "Connecting…",
    connected: "Connected", recovering: "Recovering…", ready: "Ready", safe_mode: "SAFE MODE" };
  const STATE_CLASS = { ready: "live", connected: "live", recovering: "warn",
    connecting: "warn", safe_mode: "danger", disconnected: "disabled" };

  return (
    <div className="api-page">
      <section className="card">
        <div className="card-head">
          <h1>IBKR Connection</h1>
          <span className={`mode-badge ${STATE_CLASS[state] || "disabled"}`}>
            {STATE_LABEL[state] || state}
          </span>
        </div>
        <p className="muted">
          Connect the dashboard to your local IB Gateway (paper trading). Log in to the
          Gateway in IBKR's own software first, then connect here.
        </p>
        <div className="deploy-grid" style={{ marginTop: 12 }}>
          <div><div className="stat-label">Broker</div><div className="stat-value">Interactive Brokers</div></div>
          <div>
            <div className="stat-label">Status</div>
            <div className={`stat-value ${connected ? "pos" : "neg"}`}>{connected ? "Connected" : "Not connected"}</div>
          </div>
          <div>
            <div className="stat-label">Gateway</div>
            <div className="stat-value">{info ? `${info.host}:${info.port}` : "—"}</div>
          </div>
          <div>
            <div className="stat-label">Client ID</div>
            <div className="stat-value">{info?.client_id ?? "—"}</div>
          </div>
          <div>
            <div className="stat-label">Account</div>
            <div className="stat-sub">{info?.accounts?.length ? info.accounts.join(", ") : (info?.account || "—")}</div>
          </div>
        </div>
      </section>

      <section className="card">
        <h2>Connection</h2>
        <p className="muted small">
          Gateway host/port/clientId are configured server-side via <code>IBKR_HOST</code>,
          <code>IBKR_PORT</code> (4002 = paper Gateway) and <code>IBKR_CLIENT_ID</code>. No
          credentials are stored by this app — the Gateway holds your login.
        </p>

        {err && <div className="banner error">{err}</div>}

        <div className="cred-row" style={{ marginTop: 14 }}>
          {!connected && (
            <button className="btn primary" disabled={busy} onClick={connect}>
              {busy ? "Connecting…" : "Connect"}
            </button>
          )}
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Tastytrade Market Data</h2>
          <span className={`mode-badge ${tt?.configured ? "live" : "disabled"}`}>
            {tt?.configured ? "Configured" : "Not configured"}
          </span>
        </div>
        <p className="muted small">
          Primary market-data source (quotes, option chains/mids, VIX, history). Create an
          OAuth personal grant at my.tastytrade.com → API access, then paste the client
          secret and refresh token here. Stored server-side in <code>.env</code>; IBKR is
          used as fallback while its Gateway is connected.
        </p>
        <div className="row gap center" style={{ marginTop: 10 }}>
          <input className="input" type="password" placeholder={tt?.has_secret ? "Provider secret (saved)" : "Provider secret"}
            value={ttSecret} onChange={(e) => setTtSecret(e.target.value)} />
          <input className="input" type="password" placeholder={tt?.has_refresh ? "Refresh token (saved)" : "Refresh token"}
            value={ttRefresh} onChange={(e) => setTtRefresh(e.target.value)} />
          <button className="btn primary" onClick={saveTt}>Save</button>
        </div>
        {ttMsg && <div className="muted small" style={{ marginTop: 8 }}>{ttMsg}</div>}
      </section>
    </div>
  );
}
