import { useEffect, useState } from "react";
import { api } from "../api.js";

// Dashboard header chips separating the two health signals that used to be conflated:
// Data (which market-data source is actually serving) and Broker (IBKR Gateway, orders).
const DATA_LABEL = { tastytrade: "Tastytrade", ibkr: "IBKR fallback" };

export default function DataBrokerChips() {
  const [tt, setTt] = useState(null);
  const [auth, setAuth] = useState(null);

  useEffect(() => {
    const load = () => {
      api.ttStatus().then(setTt).catch(() => setTt(null));
      api.authStatus().then(setAuth).catch(() => setAuth(null));
    };
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const dataTone = tt == null ? "idle"
    : tt.serving === "tastytrade" ? "live"
    : tt.serving === "ibkr" ? "warn"
    : tt.configured ? "idle" : "down";
  const dataText = tt == null ? "…"
    : tt.serving !== "idle" ? DATA_LABEL[tt.serving]
    : tt.configured ? "Tastytrade · idle" : "not configured";

  const brokerReady = auth?.conn_state === "ready" || (auth?.connected && !auth?.conn_state);
  const brokerText = auth == null ? "…"
    : brokerReady ? "Gateway connected"
    : auth.conn_state === "safe_mode" ? "SAFE MODE"
    : "disconnected";

  return (
    <div className="src-chips">
      <span className={`pill tiny ${dataTone}`}>Data: {dataText}</span>
      <span className={`pill tiny ${brokerReady ? "live" : "down"}`}>Broker: {brokerText}</span>
    </div>
  );
}
