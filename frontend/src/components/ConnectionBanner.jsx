// Amber app-level banner shown while the IBKR connection is not READY (and not SAFE_MODE,
// which has its own red banner). The backend supervisor is reconnecting in the background.
const RECONNECTING = new Set(["disconnected", "connecting", "recovering"]);

export default function ConnectionBanner({ auth }) {
  if (!auth || auth.safe_mode) return null;            // SAFE_MODE banner takes precedence
  const state = auth.conn_state || (auth.connected ? "connected" : "disconnected");
  if (!RECONNECTING.has(state)) return null;
  const msg = state === "recovering"
    ? "Reconnected — recovering…"
    : "IBKR disconnected — reconnecting…";
  return <div className="reconnect-banner">⚡ {msg}</div>;
}
