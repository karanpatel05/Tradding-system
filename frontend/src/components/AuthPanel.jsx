import { useState } from "react";
import { api } from "../api.js";

// Front-page landing + auth gate. Split layout: a branding/hero panel on the
// left, a dedicated authentication section on the right. Accepts a Tastytrade
// OAuth refresh token plus (optionally) the OAuth client secret. Both are sent
// to the server once and used there to build the session — neither persists in
// JS nor is ever returned to the browser.
export default function AuthPanel({ status, onAuthed }) {
  const [token, setToken] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // The secret is required only when the server has none configured.
  const secretRequired = !status.oauth_configured;
  const canSubmit = token.trim().length >= 8 && (!secretRequired || secret.trim().length > 0);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.login(token.trim(), secret);
      onAuthed();
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="landing">
      {/* Branding / hero */}
      <section className="landing-hero">
        <div className="hero-brand">⚡ Multi-Strategy Trading System</div>
        <h1 className="hero-title">
          Trade five strategies.
          <br />
          One live cockpit.
        </h1>
        <p className="hero-sub">
          Connect your Tastytrade account to stream live index levels, run strategies
          concurrently, and keep a kill switch one click away.
        </p>
        <ul className="hero-points">
          <li>
            <span className="hero-dot" /> Live market overview — SPX, NDX, RUT, Dow &amp; VIX
          </li>
          <li>
            <span className="hero-dot" /> Server-side OAuth — tokens never touch the browser
          </li>
          <li>
            <span className="hero-dot" /> Real kill switch — flatten &amp; halt every worker
          </li>
        </ul>
      </section>

      {/* Authentication section */}
      <section className="landing-auth">
        <div className="auth-card">
          <div className="auth-card-head">
            <h2>Sign in</h2>
            <p className="muted">Authenticate with your Tastytrade OAuth credentials.</p>
          </div>

          {secretRequired ? (
            <div className="banner warn">
              No OAuth client secret is configured on the server — enter it below along with
              your refresh token. (Or set <code>TASTYTRADE_PROVIDER_SECRET</code> server-side.)
            </div>
          ) : (
            <div className="banner ok">
              Server OAuth secret detected — the secret field is optional (fill only to override).
            </div>
          )}

          <form onSubmit={submit}>
            <label className="label">OAuth client secret {secretRequired ? "" : "(optional)"}</label>
            <input
              type="password"
              className="input"
              placeholder={secretRequired ? "Your OAuth client secret" : "Leave blank to use server secret"}
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              disabled={busy}
              autoComplete="off"
            />

            <label className="label">Refresh token</label>
            <input
              type="password"
              className="input"
              placeholder="Paste your Tastytrade refresh token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              disabled={busy}
              autoComplete="off"
              autoFocus
            />

            {error && <div className="banner error">{error}</div>}
            <button type="submit" className="btn primary full" disabled={busy || !canSubmit}>
              {busy ? "Authenticating…" : "Connect to market →"}
            </button>
          </form>
          <p className="muted small auth-foot">
            Credentials are stored server-side only and auto-refreshed on expiry.
          </p>
        </div>
      </section>
    </div>
  );
}
