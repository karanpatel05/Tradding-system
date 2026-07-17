import { useEffect, useState } from "react";
import { api } from "../api.js";

// Broker tab: Tastytrade is the sole broker AND market-data source. Shows the account
// (masked) + probe-verified connection state, and the OAuth credential card. There is
// no interactive login — creds are an OAuth personal grant verified by a server probe.
export default function ApiTab({ onChange }) {
  const [info, setInfo] = useState(null);
  const [acct, setAcct] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      setInfo(await api.authStatus());
    } catch (e) {
      setErr(e.message);
    }
    api.account().then((d) => setAcct(d.account || d)).catch(() => {});
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
      if (res.probe && res.probe !== "ok") setTtMsg(`Saved, but Tastytrade ${res.probe} — paste BOTH values from the same grant.`);
      else setTtMsg(res.configured ? (res.probe === "ok" ? "Saved — verified with Tastytrade ✓" : "Saved.") : "Saved.");
      onChange?.();
    } catch (e) { setTtMsg(e.message || "Save failed"); }
  };

  const reprobe = async () => {
    setErr(null); setBusy(true);
    try {
      const res = await api.connect();     // forces a fresh Tastytrade probe
      setInfo(res);
      if (!res.connected) setErr(res.detail || "Tastytrade credentials not verified.");
      onChange?.();
    } catch (e) {
      setErr(e.message || "Probe failed");
    } finally {
      setBusy(false);
    }
  };

  const connected = !!info?.connected;
  const state = info?.conn_state || (connected ? "ready" : "disconnected");
  const STATE_LABEL = { disconnected: "Disconnected", ready: "Ready", safe_mode: "SAFE MODE" };
  const STATE_CLASS = { ready: "live", safe_mode: "danger", disconnected: "disabled" };
  const money = (n) => (n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);

  return (
    <div className="api-page">
      <section className="card">
        <div className="card-head">
          <h1>Broker — Tastytrade</h1>
          <span className={`mode-badge ${STATE_CLASS[state] || "disabled"}`}>
            {STATE_LABEL[state] || state}
          </span>
        </div>
        <p className="muted">
          Tastytrade is the broker and the market-data source. Orders only transmit when{" "}
          <code>ENABLE_LIVE_ORDERS</code> is set server-side — otherwise every strategy runs
          in paper mode with simulated fills.
        </p>
        <div className="deploy-grid" style={{ marginTop: 12 }}>
          <div><div className="stat-label">Broker</div><div className="stat-value">Tastytrade</div></div>
          <div>
            <div className="stat-label">Status</div>
            <div className={`stat-value ${connected ? "pos" : "neg"}`}>{connected ? "Verified" : "Not verified"}</div>
          </div>
          <div>
            <div className="stat-label">Account</div>
            <div className="stat-value">{info?.account || "—"}</div>
          </div>
          <div>
            <div className="stat-label">Net liq</div>
            <div className="stat-value">{money(acct?.net_liquidation)}</div>
          </div>
          <div>
            <div className="stat-label">Buying power</div>
            <div className="stat-sub">{money(acct?.buying_power)}</div>
          </div>
        </div>
        {err && <div className="banner error" style={{ marginTop: 10 }}>{err}</div>}
        {!connected && (
          <div className="cred-row" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy} onClick={reprobe}>
              {busy ? "Checking…" : "Re-check connection"}
            </button>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Tastytrade Credentials</h2>
          <span className={`mode-badge ${
            tt?.probe === "ok" ? "live"
              : tt?.probe ? "danger"
              : tt?.configured ? "warn" : "disabled"}`}>
            {tt?.probe === "ok" ? "Connected ✓"
              : tt?.probe ? "Rejected"
              : tt?.configured ? "Configured (unverified)" : "Not configured"}
          </span>
        </div>
        <div className="muted small">
          Data serving: <b>{tt?.serving === "tastytrade" ? "Tastytrade" : "idle"}</b>
          {tt?.probe && tt.probe !== "ok" && <span className="neg"> · {tt.probe}</span>}
        </div>
        <p className="muted small">
          OAuth personal grant from my.tastytrade.com → API access: paste the client secret
          and refresh token (both from the same grant). Stored server-side in{" "}
          <code>.env</code>; verified against Tastytrade on save.
        </p>
        <div className="row gap center" style={{ marginTop: 10 }}>
          <input className="input" type="password" placeholder={tt?.has_secret ? "Provider secret (saved)" : "Provider secret"}
            value={ttSecret} onChange={(e) => setTtSecret(e.target.value)} />
          <input className="input" type="password" placeholder={tt?.has_refresh ? "Refresh token (saved)" : "Refresh token"}
            value={ttRefresh} onChange={(e) => setTtRefresh(e.target.value)} />
          <button className="btn primary" onClick={saveTt}>Save</button>
          <button className="btn ghost" onClick={() => api.ttStatus(true).then(setTt).catch(() => {})}>
            Test connection
          </button>
        </div>
        {ttMsg && <div className="muted small" style={{ marginTop: 8 }}>{ttMsg}</div>}
      </section>
    </div>
  );
}
