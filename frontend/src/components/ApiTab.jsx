import { useEffect, useState } from "react";
import { api, getAppToken, setAppToken } from "../api.js";

// Broker tab: Alpaca paper is the broker (orders, account); Tastytrade is market data
// only. Shows probe-verified connection state, read-only Tastytrade and Alpaca API status
// (masked Alpaca account), and the order-routing gate. No credentials are entered here —
// they live in backend/.env only.
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

  // Status only — credentials live in backend/.env and are never entered or shown here.
  const [tt, setTt] = useState(null);
  const [ttChecking, setTtChecking] = useState(false);
  const loadTt = (probe) =>
    api
      .ttStatus(probe)
      .then(setTt)
      .catch(() => {});
  const testTt = async () => {
    setTtChecking(true);
    try {
      await loadTt(true);
      onChange?.();
    } finally {
      setTtChecking(false);
    }
  };

  const [alpaca, setAlpaca] = useState(null);
  const [alpacaChecking, setAlpacaChecking] = useState(false);
  const loadAlpaca = (probe) =>
    api
      .alpacaStatus(probe)
      .then(setAlpaca)
      .catch(() => {});
  const testAlpaca = async () => {
    setAlpacaChecking(true);
    try {
      await loadAlpaca(true);
    } finally {
      setAlpacaChecking(false);
    }
  };

  useEffect(() => {
    loadTt();
    loadAlpaca();
    const id = setInterval(() => {
      loadTt();
      loadAlpaca();
    }, 30000);
    return () => clearInterval(id);
  }, []);

  // --- App access token (gates the dangerous mutating actions) ---
  const [appToken, setAppTokenInput] = useState(getAppToken());
  const [tokenMsg, setTokenMsg] = useState(null);
  const hasToken = !!getAppToken();
  const saveAppToken = () => {
    setAppToken(appToken.trim());
    setTokenMsg(appToken.trim() ? "Saved on this device." : "Cleared.");
  };

  // --- Order routing (ENABLE_LIVE_ORDERS master gate) ---
  const [health, setHealth] = useState(null);
  const [arming, setArming] = useState(false); // confirm row visible
  const [confirmText, setConfirmText] = useState("");
  const [ordersMsg, setOrdersMsg] = useState(null);
  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch(() => {});
  }, []);

  const liveOn = !!health?.live_orders_enabled;
  const setLive = async (enabled) => {
    setOrdersMsg(null);
    try {
      const res = await api.setLiveOrders(enabled);
      setHealth((h) => ({
        ...(h || {}),
        live_orders_enabled: res.live_orders_enabled,
      }));
      setArming(false);
      setConfirmText("");
      onChange?.();
    } catch (e) {
      setOrdersMsg(e.message || "Update failed");
    }
  };

  const reprobe = async () => {
    setErr(null);
    setBusy(true);
    try {
      const res = await api.connect(); // forces a fresh Tastytrade probe
      setInfo(res);
      if (!res.connected)
        setErr(res.detail || "Tastytrade credentials not verified.");
      onChange?.();
    } catch (e) {
      setErr(e.message || "Probe failed");
    } finally {
      setBusy(false);
    }
  };

  const connected = !!info?.connected;
  const state = info?.conn_state || (connected ? "ready" : "disconnected");
  const STATE_LABEL = {
    disconnected: "Disconnected",
    ready: "Ready",
    safe_mode: "SAFE MODE",
  };
  const STATE_CLASS = {
    ready: "live",
    safe_mode: "danger",
    disconnected: "disabled",
  };
  const money = (n) =>
    n == null
      ? "—"
      : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

  return (
    <div className="api-page">
      <header className="page-head">
        <div>
          <h1>Connections</h1>
          <p className="page-description">
            Your broker, market data and execution services in one place.
          </p>
        </div>
      </header>
      <div className="connections-grid">
        <section className="card">
          <div className="card-head">
            <h2>Market data — Tastytrade</h2>
            <span className={`mode-badge ${STATE_CLASS[state] || "disabled"}`}>
              {STATE_LABEL[state] || state}
            </span>
          </div>
          <p className="muted">
            Tastytrade supplies market data only. Orders and account balances
            come from your Alpaca account (see below).
          </p>
          <div className="deploy-grid" style={{ marginTop: 12 }}>
            <div>
              <div className="stat-label">Source</div>
              <div className="stat-value">Tastytrade</div>
            </div>
            <div>
              <div className="stat-label">Status</div>
              <div className={`stat-value ${connected ? "pos" : "neg"}`}>
                {connected ? "Verified" : "Not verified"}
              </div>
            </div>
          </div>
          {err && (
            <div className="banner error" style={{ marginTop: 10 }}>
              {err}
            </div>
          )}
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
            <h2>Tastytrade API</h2>
            <span
              className={`mode-badge ${
                tt?.probe === "ok"
                  ? "live"
                  : tt?.probe
                    ? "danger"
                    : tt?.configured
                      ? "warn"
                      : "disabled"
              }`}
            >
              {tt?.probe === "ok"
                ? "Connected ✓"
                : tt?.probe
                  ? "Rejected"
                  : tt?.configured
                    ? "Configured (unverified)"
                    : "Not configured"}
            </span>
          </div>
          <div className="deploy-grid" style={{ marginTop: 8 }}>
            <div>
              <div className="stat-label">Credentials</div>
              <div className="stat-value">
                {tt == null ? "—" : tt.configured ? "Set in .env" : "Missing"}
              </div>
            </div>
            <div>
              <div className="stat-label">Data serving</div>
              <div className="stat-value">
                {tt?.serving === "tastytrade" ? "Tastytrade" : "idle"}
              </div>
            </div>
          </div>
          {tt?.probe && tt.probe !== "ok" && (
            <div className="banner error" style={{ marginTop: 10 }}>
              Tastytrade: {tt.probe}
            </div>
          )}
          <p className="muted small">
            Credentials are read from <code>TT_PROVIDER_SECRET</code> /{" "}
            <code>TT_REFRESH_TOKEN</code> in <code>backend/.env</code> (restart
            the backend after editing).
          </p>
          <button className="btn ghost" disabled={ttChecking} onClick={testTt}>
            {ttChecking ? "Checking…" : "Test connection"}
          </button>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Alpaca API{alpaca?.paper ? " (paper)" : ""}</h2>
            <span
              className={`mode-badge ${
                alpaca?.status === "ok"
                  ? "live"
                  : alpaca?.status === "rejected" || alpaca?.status === "error"
                    ? "danger"
                    : "disabled"
              }`}
            >
              {alpaca?.status === "ok"
                ? "Connected ✓"
                : alpaca?.status === "rejected"
                  ? "Rejected"
                  : alpaca?.status === "error"
                    ? "Error"
                    : alpaca
                      ? "Not configured"
                      : "—"}
            </span>
          </div>
          <div className="deploy-grid" style={{ marginTop: 8 }}>
            <div>
              <div className="stat-label">Account</div>
              <div className="stat-value">{alpaca?.account || "—"}</div>
            </div>
            <div>
              <div className="stat-label">Status</div>
              <div className="stat-value">{alpaca?.account_status || "—"}</div>
            </div>
            <div>
              <div className="stat-label">Equity</div>
              <div className="stat-value">{money(alpaca?.equity)}</div>
            </div>
            <div>
              <div className="stat-label">Buying power</div>
              <div className="stat-sub">{money(alpaca?.buying_power)}</div>
            </div>
          </div>
          {alpaca?.detail && (
            <div className="banner error" style={{ marginTop: 10 }}>
              Alpaca: {alpaca.detail}
            </div>
          )}
          <p className="muted small">
            Alpaca supplies paper execution and account balances. Keys are read
            from <code>APCA_API_KEY_ID</code> / <code>APCA_API_SECRET_KEY</code>{" "}
            in <code>backend/.env</code>.
          </p>
          <button
            className="btn ghost"
            disabled={alpacaChecking}
            onClick={testAlpaca}
          >
            {alpacaChecking ? "Checking…" : "Test connection"}
          </button>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>App Access Token</h2>
            <span className={`mode-badge ${hasToken ? "live" : "warn"}`}>
              {hasToken ? "Set" : "Not set"}
            </span>
          </div>
          <p className="muted small">
            Gates the dangerous actions — sending orders to Alpaca paper, the
            kill switch, activating a strategy, and resuming from SAFE MODE.
            Must match <code>APP_AUTH_TOKEN</code> in <code>backend/.env</code>.
            Stored only in this browser; until it's set (matching the server)
            those actions return 401.
          </p>
          <div className="row gap center" style={{ marginTop: 10 }}>
            <input
              className="input"
              type="password"
              placeholder={hasToken ? "App token (saved)" : "App token"}
              value={appToken}
              onChange={(e) => setAppTokenInput(e.target.value)}
            />
            <button className="btn primary" onClick={saveAppToken}>
              Save
            </button>
          </div>
          {tokenMsg && (
            <div className="muted small" style={{ marginTop: 8 }}>
              {tokenMsg}
            </div>
          )}
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Order Routing</h2>
            <span className={`mode-badge ${liveOn ? "danger" : "disabled"}`}>
              {liveOn ? "ALPACA PAPER" : "SIMULATED"}
            </span>
          </div>
          <p className="muted small">
            {liveOn ? (
              <>
                Orders go to your Alpaca <b>paper</b> account (live trading
                locked)
              </>
            ) : (
              "Simulated — orders never leave the app"
            )}
            . The setting persists in <code>.env</code> across restarts. SAFE
            MODE and the kill switch are unaffected.
          </p>
          {!liveOn && !arming && (
            <button
              className="btn primary"
              style={{ marginTop: 10 }}
              onClick={() => {
                setOrdersMsg(null);
                setArming(true);
              }}
            >
              Send orders to Alpaca paper…
            </button>
          )}
          {!liveOn && arming && (
            <div className="row gap center" style={{ marginTop: 10 }}>
              <input
                className="input"
                placeholder="Type LIVE to confirm"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
              />
              <button
                className="btn danger"
                disabled={confirmText !== "LIVE"}
                onClick={() => setLive(true)}
              >
                Send orders to Alpaca paper
              </button>
              <button
                className="btn ghost"
                onClick={() => {
                  setOrdersMsg(null);
                  setArming(false);
                  setConfirmText("");
                }}
              >
                Cancel
              </button>
            </div>
          )}
          {liveOn && (
            <button
              className="btn"
              style={{ marginTop: 10 }}
              onClick={() => setLive(false)}
            >
              Switch to simulation
            </button>
          )}
          {ordersMsg && (
            <div className="banner error" style={{ marginTop: 10 }}>
              {ordersMsg}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
