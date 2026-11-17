// Amber app-level banner shown when the Tastytrade credential probe is failing (and
// not SAFE_MODE, which has its own red banner). Orders and data pause until fixed.
export default function ConnectionBanner({ auth }) {
  if (!auth || auth.safe_mode) return null;            // SAFE_MODE banner takes precedence
  const state = auth.conn_state || (auth.connected ? "ready" : "disconnected");
  if (state !== "disconnected") return null;
  return (
    <div className="reconnect-banner">
      ⚡ Tastytrade credentials not verified — check TT_PROVIDER_SECRET / TT_REFRESH_TOKEN in backend/.env.
    </div>
  );
}
