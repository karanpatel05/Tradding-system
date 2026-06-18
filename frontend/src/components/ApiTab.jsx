import { useEffect, useState } from "react";
import { api } from "../api.js";

// API connection tab: shows broker connection status and the stored OAuth secret
// + refresh token in editable fields. Saving re-runs the login (which builds the
// session and persists the credentials to the server's local 0600 store), so the
// connection survives restarts. Secret/token are masked by default with a reveal.
export default function ApiTab({ onChange }) {
  const [info, setInfo] = useState(null);
  const [secret, setSecret] = useState("");
  const [token, setToken] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      const c = await api.getCredentials();
      setInfo(c);
      setSecret(c.provider_secret || "");
      setToken(c.refresh_token || "");
    } catch (e) {
      setErr(e.message);
    }
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setMsg(null); setErr(null); setBusy(true);
    try {
      const res = await api.saveCredentials(secret, token.trim());
      // Saved either way — values are persisted locally even if the connect failed.
      setInfo(res);
      if (res.provider_secret) setSecret(res.provider_secret);
      if (res.refresh_token) setToken(res.refresh_token);
      if (res.connected) setMsg("Saved & connected — credentials stored locally and reused on restart.");
      else setErr(res.detail || "Saved locally, but not connected yet.");
      onChange?.();
    } catch (e) {
      setErr(e.message || "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setMsg(null); setErr(null); setBusy(true);
    try {
      await api.logout();
      setMsg("Disconnected.");
      await load();
      onChange?.();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const connected = !!info?.connected;

  return (
    <div className="api-page">
      <section className="card">
        <div className="card-head">
          <h1>API Connection</h1>
          <span className={`mode-badge ${connected ? "live" : "disabled"}`}>
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
        <p className="muted">Connect the dashboard to your Tastytrade account via OAuth.</p>
        <div className="deploy-grid" style={{ marginTop: 12 }}>
          <div><div className="stat-label">Broker</div><div className="stat-value">Tastytrade</div></div>
          <div>
            <div className="stat-label">Status</div>
            <div className={`stat-value ${connected ? "pos" : "neg"}`}>{connected ? "Connected" : "Not connected"}</div>
          </div>
          <div>
            <div className="stat-label">OAuth secret</div>
            <div className="stat-value">{info?.oauth_configured ? "Set" : "Missing"}</div>
          </div>
          <div>
            <div className="stat-label">Accounts</div>
            <div className="stat-sub">{info?.accounts?.length ? info.accounts.join(", ") : "—"}</div>
          </div>
        </div>
      </section>

      <section className="card">
        <h2>Credentials</h2>
        <p className="muted small">
          Stored locally on the server (<code>.session_store.json</code>, owner-only) and reused on
          restart. Edit a field and Save to update. Saving re-connects with the new values.
        </p>

        <label className="label" style={{ marginTop: 12 }}>OAuth client secret (Secret ID)</label>
        <div className="cred-row">
          <input className="input" type={showSecret ? "text" : "password"} value={secret}
            onChange={(e) => setSecret(e.target.value)} placeholder="OAuth client secret"
            autoComplete="off" disabled={busy} />
          <button className="btn" type="button" onClick={() => setShowSecret((s) => !s)}>
            {showSecret ? "Hide" : "Show"}
          </button>
        </div>

        <label className="label" style={{ marginTop: 12 }}>Refresh token</label>
        <div className="cred-row">
          <input className="input" type={showToken ? "text" : "password"} value={token}
            onChange={(e) => setToken(e.target.value)} placeholder="Tastytrade refresh token"
            autoComplete="off" disabled={busy} />
          <button className="btn" type="button" onClick={() => setShowToken((s) => !s)}>
            {showToken ? "Hide" : "Show"}
          </button>
        </div>

        {err && <div className="banner error">{err}</div>}
        {msg && <div className="banner ok">{msg}</div>}

        <div className="cred-row" style={{ marginTop: 14 }}>
          <button className="btn primary" disabled={busy || token.trim().length < 8} onClick={save}>
            {busy ? "Saving…" : "Save & Connect"}
          </button>
          {connected && (
            <button className="btn danger" disabled={busy} onClick={disconnect}>Disconnect</button>
          )}
        </div>
      </section>
    </div>
  );
}
